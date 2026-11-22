# A plot with no device opened, in a font family pdf() does not know. In
# RStudio this draws (fontconfig substitutes a font); under Rscript's default
# pdf() device it stops with "invalid font type".
plot(1:3, main = "a", family = "Arial")
text(2, 2, "label", family = "Arial")
dir.create("results", showWarnings = FALSE)
writeLines("ok", "results/done.txt")
