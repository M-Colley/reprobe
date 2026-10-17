# The RStudio idiom: set the working directory to this script's folder by asking
# the IDE which file is open. Under Rscript there is no IDE, so this fails —
# and only this: with the IDE's answer the script reads its data and finishes.
setwd(dirname(rstudioapi::getActiveDocumentContext()$path))
d <- read.csv("data.csv")
writeLines(as.character(d$x + 1), "answer.txt")
