
#!/usr/bin/env python3

from __future__ import print_function, absolute_import, division

import logging

import os
import sys
import errno

from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
from passthrough import Passthrough

class FuseDemo(LoggingMixIn, Passthrough):
      """A simple passthrough filesystem with logging, used for Part 1."""

def main(root, mountpoint):
    FUSE(FuseDemo(root), mountpoint, nothreads=True, foreground=True)

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print('Usage: python3 fuse_demo.py <source> <mount>')
        sys.exit(1)
    logging.basicConfig(level=logging.DEBUG)
    main(sys.argv[1], sys.argv[2])
