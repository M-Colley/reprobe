# An renv project: its one package comes from renv::restore() of renv.lock, in
# the install phase, into renv's project library. The package is loaded FROM
# that library (lib.loc), so a pass needs the library to still be usable in the
# offline run phase — renv's default links it into a cache the run never sees.
lib <- dirname(list.files("renv/library", pattern = "^praise$", recursive = TRUE,
                          include.dirs = TRUE, full.names = TRUE))
stopifnot(length(lib) == 1)
library(praise, lib.loc = lib)
stopifnot(is.function(praise))
dir.create("results", showWarnings = FALSE)
writeLines(as.character(packageVersion("praise", lib.loc = lib)), "results/version.txt")
