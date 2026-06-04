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
        self.source_map = {}

        self.fd = 0

        # passthrough deals with all the file opersa
        self.passthrough1 = Passthrough(root1)
        self.passthrough2 = Passthrough(root2)

        # memory fs for new files created in the mount point - these die when the fs is unmounted
        self.memory = Memory()
        
        now = time()
        self.source_map['/'] = {
            'root': None,
            'st_mode': S_IFDIR | 0o755,
            'st_ctime': now,
            'st_mtime': now,
            'st_atime': now,
            'st_nlink': 2,
            'st_size': 0,
            # we need to get the user and group ids of the user who owns the mount point.
            'st_uid': os.stat(argv[3]).st_uid,
            'st_gid': os.stat(argv[3]).st_gid,
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
                        'st_nlink': 1,
                        'st_size': os.path.getsize(path),
                        'st_uid': os.stat(path).st_uid,
                        'st_gid': os.stat(path).st_gid,
                    }
                    self.source_map['/']['st_nlink'] += 1
        
        # lets list out all the files in the source directories and log them
        logger.info("Files in source directories:")
        for path in self.source_map:
            logger.info(path)


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

    ## fs stuff

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
            # no nested dirs
            if file_path.startswith(path) and file_path != path:
                relative_path = os.path.relpath(file_path, path)
                if '/' not in relative_path:
                    files.append(relative_path)

        logger.debug("files in directory: %s", files)
        return files

    ## file stuff

    def open(self, path, flags):
        if path not in self.source_map:
            raise FuseOSError(errno.ENOENT)

        root = self.source_map[path]['root']
        if root is None:
            # memory file, fd is just a counter
            self.fd += 1
            return self.fd
        else:
            full_path = os.path.join(root, path[1:])  # strip leading /
            return os.open(full_path, flags)

    def read(self, path, size, offset, fh):
        logger.debug("read called with path: %s", path)
        if path not in self.source_map:
            raise FuseOSError(errno.ENOENT)

        root = self.source_map[path]['root']
        if root is None:
            # this is a file created in memory
            return self.memory.read(path, size, offset, fh)
        elif root == self.passthrough1.root:
            return self.passthrough1.read(path, size, offset, fh)
        elif root == self.passthrough2.root:
            return self.passthrough2.read(path, size, offset, fh)
        else:
            raise FuseOSError(errno.EACCES)
    
    def create(self, path, mode):
        logger.debug("create called with path: %s", path)
        if path in self.source_map:
            raise FuseOSError(errno.EEXIST)

        # create a new file in memory
        self.memory.create(path, mode)

        # add it to the source map
        now = time()
        self.source_map[path] = {
            'root': None,
            'st_mode': S_IFREG | mode,
            'st_ctime': now,
            'st_mtime': now,
            'st_atime': now,
            'st_nlink': 1,
            'st_size': 0,
            'st_uid': os.getuid(),
            'st_gid': os.getgid()
        }
        self.fd += 1
        return self.fd

    def write(self, path, data, offset, fh):
        logger.debug("write called with path: %s", path)
        if path not in self.source_map:
            raise FuseOSError(errno.ENOENT)

        root = self.source_map[path]['root']
        if root is None:
            # ensure data is bytes, ensure existing data is bytes
            existing = self.memory.data[path]
            if isinstance(existing, str):
                existing = existing.encode()
            if isinstance(data, str):
                data = data.encode()
            self.memory.data[path] = existing[:offset] + data
            self.memory.files[path]['st_size'] = len(self.memory.data[path])
            self.source_map[path]['st_size'] = self.memory.files[path]['st_size']
            return len(data)
        else:
            self.source_map[path]['st_mtime'] = time()
            self.source_map[path]['st_atime'] = time()
            self.source_map[path]['st_size'] = max(self.source_map[path]['st_size'], offset + len(data))
            # file in source, passthrough write
            if root == self.passthrough1.root:
                return self.passthrough1.write(path, data, offset, fh)
            elif root == self.passthrough2.root:
                return self.passthrough2.write(path, data, offset, fh)
            else:
                raise FuseOSError(errno.EACCES)

    def truncate(self, path, length, fh=None):
        logger.debug("truncate called with path: %s", path)
        if path not in self.source_map:
            raise FuseOSError(errno.ENOENT)

        root = self.source_map[path]['root']
        if root is None:
            # memory file, just update the data and size
            existing = self.memory.data[path]
            if isinstance(existing, str):
                existing = existing.encode()
            self.memory.data[path] = existing[:length]
            self.memory.files[path]['st_size'] = len(self.memory.data[path])
            self.source_map[path]['st_size'] = self.memory.files[path]['st_size']
            return 0
        elif root == self.passthrough1.root:
            return self.passthrough1.truncate(path, length, fh)
        elif root == self.passthrough2.root:
            return self.passthrough2.truncate(path, length, fh)
        else:
            raise FuseOSError(errno.EACCES)
    
    def flush(self, path, fh):
        logger.debug("flush called with path: %s", path)
        if path not in self.source_map:
            raise FuseOSError(errno.ENOENT)

        root = self.source_map[path]['root']
        if root is None:
            # memory file, nothing to flush
            return 0
        elif root == self.passthrough1.root:
            return self.passthrough1.flush(path, fh)
        elif root == self.passthrough2.root:
            return self.passthrough2.flush(path, fh)
        else:
            raise FuseOSError(errno.EACCES)
    
    def release(self, path, fh):
        logger.debug("release called with path: %s", path)
        if path not in self.source_map:
            raise FuseOSError(errno.ENOENT)

        root = self.source_map[path]['root']
        if root is None:
            # memory file, nothing to release
            return 0
        elif root == self.passthrough1.root:
            return self.passthrough1.release(path, fh)
        elif root == self.passthrough2.root:
            return self.passthrough2.release(path, fh)
        else:
            raise FuseOSError(errno.EACCES)
    
    def unlink(self, path):
        logger.debug("unlink called with path: %s", path)
        if path not in self.source_map:
            raise FuseOSError(errno.ENOENT)

        root = self.source_map[path]['root']
        if root is None:
            # memory file, just remove it from the source map and memory
            del self.source_map[path]
            self.memory.unlink(path)
            return 0
        else:
            # delete the file from the source map and passthrough
            if root == self.passthrough1.root:
                self.passthrough1.unlink(path)
            elif root == self.passthrough2.root:
                self.passthrough2.unlink(path)
            else:
                raise FuseOSError(errno.EACCES)
            del self.source_map[path]


def main(root1, root2, mountpoint):
    FUSE(HybridFS(root1, root2), mountpoint, nothreads=True, foreground=True)

if __name__ == '__main__':
    if len(argv) != 4:
        print('usage: %s <root1> <root2> <mountpoint>' % argv[0])
        exit(1)

    logging.basicConfig(level=logging.DEBUG)
    main(argv[1], argv[2], argv[3])
