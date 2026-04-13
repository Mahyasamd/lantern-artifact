#!/bin/bash
for d in cts_mutated_*; do
  cp cts/standalone/index7.html "$d/standalone/"
done

