import sys
from pathlib import Path

# Allow `import trend` without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
