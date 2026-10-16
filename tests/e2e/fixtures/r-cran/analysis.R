# A tiny artifact whose one CRAN package is NOT in the base image: the run only
# passes if the install phase fetched it from the pinned snapshot.
library(praise)
stopifnot(is.function(praise))
dir.create("results", showWarnings = FALSE)
writeLines(as.character(nchar("abc")), "results/n.txt")
