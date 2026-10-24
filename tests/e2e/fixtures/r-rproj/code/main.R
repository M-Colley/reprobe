# Written in RStudio with analysis.Rproj open: the working directory is code/,
# so the helper and the data are addressed relative to it.
source("helpers.R")
d <- read.csv("../data/x.csv")
dir.create("out", showWarnings = FALSE)
writeLines(as.character(total(d$x)), "out/sum.txt")
