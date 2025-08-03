# .vscode/run_module.py
import sys, runpy

if __name__ == "__main__":
    mod = sys.argv[1]
    # Preserve a copy of the original argv so Scalene can still serialize it.
    original_argv = sys.argv[:]
    sys.argv = [mod] + sys.argv[2:]
    runpy.run_module(mod, run_name="__main__")