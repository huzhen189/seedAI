import os
import sys

# 让 pytest 能 import `app` 包(backend/business 作为根目录)。
sys.path.insert(0, os.path.dirname(__file__))
