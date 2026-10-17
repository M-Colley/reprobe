# renv.lock alone, no activation: restore puts praise in the harness library,
# which the run puts on the path. Loaded through a variable so the import scan
# cannot install it by itself — only the restore can make this pass.
pkg <- "praise"
library(pkg, character.only = TRUE)
dir.create("results", showWarnings = FALSE)
writeLines(as.character(packageVersion(pkg)), "results/version.txt")
