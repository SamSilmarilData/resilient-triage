# Bug-hunt repros

Evidence for the wayfinder ticket "Bug hunt across src/" (#3). Throwaway branch; never merged.

Each test asserts the **correct** behaviour, so a failing test confirms the defect.
`test_r10_*` is expected to pass (it ruled a suspected defect out).
`conftest.py` is a verbatim copy of `tests/conftest.py`. R12 needs a local Redis.

    GROQ_API_KEY= .venv/bin/pytest -c pyproject.toml --rootdir=. -p no:cacheprovider docs/research/bug-hunt -q

Run against `main` at the commit this branch is based on: 14 failed, 1 passed.
