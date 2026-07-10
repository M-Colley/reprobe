"""A deliberately broken analysis — imports a missing package and would raise.
Used to confirm reprobe reports a clean FAIL (not a crash) and that the LLM
diagnosis path engages."""

import pandas  # noqa: F401  (not in a stdlib-only env -> ModuleNotFoundError)

raise SystemExit("this fixture is supposed to fail")
