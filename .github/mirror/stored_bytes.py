"""Print the total apparent size in bytes of the regular files under DIR.

  python3 -I stored_bytes.py DIR

A file that disappears between listing and stat (a tool renaming or deleting
its temporary files) is skipped. Any other error, or a DIR that is not an
existing directory, exits non-zero with no number printed.
"""
import os
import stat
import sys


def walk_error(err):
    if not isinstance(err, FileNotFoundError):
        raise err


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = argv[1]
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise NotADirectoryError(root)
        total = 0
        for parent, _dirs, files in os.walk(root, onerror=walk_error):
            for name in files:
                try:
                    st = os.lstat(os.path.join(parent, name))
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
    except OSError as e:
        print(f'stored_bytes: {e}', file=sys.stderr)
        return 1
    print(total)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
