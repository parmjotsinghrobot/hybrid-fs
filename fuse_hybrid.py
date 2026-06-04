#!/usr/bin/env python
from memory import Memory
from passthrough import Passthrough

import os
import errno
import logging
from collections import defaultdict
from stat import S_IFDIR, S_IFLNK, S_IFREG
from time import time
from sys import argv, exit
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn

# logger
logger = logging.getLogger('hybridfs')

## requirements
# enable the FUSE system to mount two source directories into a single mount point
# distinguish between files originating from the two source directories, and newly created files in the mount point
# newly created files should be stored in memory and not on disk
# if a pre-existing file is modified, the changes should pass through to the underlying file system

## working
# the file system should work correctly with cat, ls and rm
# files in source directories should be visible in the mount point
# files created by the file system should have the user and group ids

class HybridFS(LoggingMixIn, Operations):
    def __init__(self, root1, root2):
        # we need to map a file path to what root it belongs to
        # the source map will have the relative path as the key, and the following dictionary as the value:
        # {
        #     'root': root1 or root2,
        #     'st_mode': st_mode (dir or file),
        #     'st_ctime': st_ctime,
        #     'st_mtime': st_mtime,
        #     'st_atime': st_atime,
        #     'st_nlink': st_nlink,
        # }
        self.source_map = {}

        # open file descriptor set, if the file is opened, it gets added to this set, and when it is closed, it gets removed from this set; size of th set is the number of open files
        self.fd = {}

        # passthrough deals with all the file opersa
        self.passthrough1 = Passthrough(root1)
        self.passthrough2 = Passthrough(root2)

        # memory fs for new files created in the mount point - these die when the fs is unmounted
        self.memory = Memory()
        
        # lets add the fake root to the source map, this will be used to handle the root directory of the mount point

        # something strange that is happening with '/' is that the key is actually being converted into a relative path to my home folder, which is really weird; for example, if my home folder is /home/parmjot, then the key in the source map is actually '../../../[...]/../' instead of just '/'. 
        now = time()
        self.source_map['/'] = {
            'root': None,
            'st_mode': S_IFDIR | 0o755,
            'st_ctime': now,
            'st_mtime': now,
            'st_atime': now,
            'st_nlink': 2,
        }

        # we can populate the source map by walking through the two source directories and mapping each file to its root
        for root in [root1, root2]:
            for dirpath, dirnames, filenames in os.walk(root):
                for filename in filenames:
                    path = os.path.join(dirpath, filename)
                    self.source_map[path] = {
                        'root': root,
                        'st_mode': S_IFREG | 0o644,
                        'st_ctime': os.path.getctime(path),
                        'st_mtime': os.path.getmtime(path),
                        'st_atime': os.path.getatime(path),
                        # it might seem fair to assume that the number of links is 1 for all files, but we should actually check if the file is a hard link and set the number of links accordingly; however, for simplicity we can just set it to 1 for now and ignore hard links
                        'st_nlink': 1,
                    }
                    # we should also increment the link count for the root
                    self.source_map['/']['st_nlink'] += 1
        
        # lets list out all the files in the source directories and log them
        logger.info("Files in source directories:")
        for path in self.source_map:
            logger.info(path)

        # note - these paths include the root directory, we should strip that out now to make it easier to work with later
        # note 2 - this code breaks the '/' key, we should make sure to skip that key when we are doing this
        # note 3 - we want to keep the / preceding the relative path to make it easier to work with later, for example, when we are doing the readdir operation, we can just check if the file path starts with the specified path and is not equal to the specified path (to avoid including the directory itself in the list of files returned)
        for path in list(self.source_map.keys()):
            if path == '/':
                continue
            root = self.source_map[path]['root']
            relative_path = '/' + os.path.relpath(path, root)
            self.source_map[relative_path] = self.source_map[path]
            del self.source_map[path]

        # list them again to confirm
        logger.info("Files in source directories (relative paths):")
        for path in self.source_map:
            logger.info(path)

        # we should probably handle . and .. as well, since they are used in the readdir operation; however, .. is a bit odd since it will be reading the parent directory of the mount point, which is outside of our control; for now we can just ignore it and let it fail if it is accessed
        self.source_map['.'] = None
        self.source_map['..'] = None
        

    def getattr(self, path, fh=None):
        logger.debug("getattr called with path: %s", path)
        # logger.debug("source map: %s", self.source_map)
        if path not in self.source_map:
            raise FuseOSError(errno.ENOENT)

        return self.source_map[path]
    
    def readdir(self, path, fh):
        logger.debug("readdir called with path: %s", path)
        # we should return the list of files in the directory specified by path; we can do this by looking at the source map and finding all the files that have the specified path as a prefix; for example, if the path is /dir1, then we should return all the files that have /dir1 as a prefix in their relative path; we should also include . and .. in the list of files returned
        files = ['.', '..']
        for file_path in self.source_map:
            # we do not have nested directories, so we can just check if the file path starts with the specified path and is not equal to the specified path (to avoid including the directory itself in the list of files returned)
            # files have / in front of them
            if file_path.startswith(path) and file_path != path:
                # we should also check if the file path is directly under the specified path, and not in a subdirectory; for example, if the specified path is /dir1, then we should include /dir1/file1 in the list of files returned, but not /dir1/subdir/file2; we can do this by checking if the relative path of the file (relative to the specified path) does not contain a / character
                relative_path = os.path.relpath(file_path, path)
                if '/' not in relative_path:
                    files.append(relative_path)

        logger.debug("files in directory: %s", files)
        return files

def main(root1, root2, mountpoint):
    FUSE(HybridFS(root1, root2), mountpoint, nothreads=True, foreground=True)

if __name__ == '__main__':
    if len(argv) != 4:
        print('usage: %s <root1> <root2> <mountpoint>' % argv[0])
        exit(1)

    logging.basicConfig(level=logging.DEBUG)
    main(argv[1], argv[2], argv[3])
