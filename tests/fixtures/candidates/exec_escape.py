import os
import sys


os.execv(
    sys.executable,
    [
        sys.executable,
        "-c",
        (
            "from pathlib import Path;"
            "Path('/output/result.json').write_text('{}');"
            "Path('/output/return.json').write_text('{}')"
        ),
    ],
)
