# Python Environment

inclusion: auto

## Virtual Environment

This project uses a Python virtual environment located at `.venv/` in the project root.

**All Python commands MUST use the venv.** Do NOT use bare `python`, `pytest`, or `pip` commands.

### Correct usage:

```bash
# Run tests
.venv/bin/python -m pytest tests/ -v

# Run a Python script
.venv/bin/python script.py

# Install a package
.venv/bin/pip install package-name

# Run any Python module
.venv/bin/python -m module_name
```

### Working directory

The project root is `/home/cabbey/src/FlightCardReader/`. All relative paths in commands should be run from this directory.
