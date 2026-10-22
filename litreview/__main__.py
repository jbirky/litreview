import sys

from .cli import main

if __name__ == "__main__":  # not when a worker process imports it again (fulltext's OCR)
    sys.exit(main())
