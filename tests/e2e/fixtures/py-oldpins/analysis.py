# A 2022 artifact: pandas 1.4.3 ships wheels up to CPython 3.10 and does not
# build on 3.13, and it does not load under numpy 2 (which its open `numpy>=`
# requirement would get today).
import os
import sys

import numpy
import pandas

os.makedirs("results", exist_ok=True)
with open("results/versions.txt", "w", encoding="utf-8") as fh:
    fh.write(f"{sys.version_info[0]}.{sys.version_info[1]} {pandas.__version__} {numpy.__version__}\n")
print(pandas.DataFrame({"a": [1, 2]}).sum().iloc[0])
