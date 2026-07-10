# Example analysis (reprobe fixture)

A minimal, dependency-free stand-in for a real AutoUI analysis repository.
`01_analyze.py` reads `data/measurements.csv` and writes `results/summary.csv`
and `figures/summary.txt`. An `autoui-repro.yml` manifest declares the run plan
and expected outputs, so reprobe needs no guesswork (and no LLM) to run it.

```
reprobe run ./examples/example-python
```
