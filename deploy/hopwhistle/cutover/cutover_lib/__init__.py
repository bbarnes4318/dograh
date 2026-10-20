"""Planning, validation and recovery logic for the hopwhistle api cutover.

Standard library only: the box has python3 but no PyYAML or alembic on the
host. The shell scripts in the parent directory do the docker/compose work;
everything that decides whether a step is safe lives here so it can be tested
offline (api/tests/test_cutover_*.py).
"""
