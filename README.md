
# LANTERN 

## Dynamic Conformance Testing of WebGPU through Specification-Driven Mutation
---
> **Anonymous Artifact Submission**
>
> This artifact accompanies an anonymous submission to the ISSRE 2026.
> All identifying information has been removed for double-blind review.


## Overview

LANTERN is a specification-guided dynamic conformance testing framework for WebGPU.
It systematically mutates WebGPU Conformance Test Suite (CTS) test cases using:

- Explicit rules extracted directly from the official WebGPU specification (WebIDL)
- Implicit semantic and stateful constraints inferred from the specification text with LLM assistance and then manually validated

LANTERN performs AST-level, rule-aware mutations and executes the resulting tests on instrumented Chromium builds to expose validation failures and crashes in WebGPU implementations. It supports valid mutation (rule-repairing), invalid mutation (rule-violating), single-folder execution, parallel fuzzing, and manual reproduction in Chromium (without the fuzzer).
LANTERN does not replace the WebGPU CTS; instead, it extends CTS by dynamically generating additional test variants derived from the same specification.

---

## Artifact Scope

This artifact enables:

- Inspection of explicit and implicit WebGPU rules
- Deterministic reproduction of CTS mutations
- Large-scale fuzzing on Chromium
- Manual crash reproduction without the fuzzer

---

## System Requirements

- Linux (tested on Ubuntu 22.04 / 24.04)
- Python ≥ 3.8
- npm
- esbuild
- ASan-instrumented Chromium binary build with WebGPU enabled 
- Approximately 200GB of disk space recommended for Chromium build artifacts and logs

---

## Repository Structure

```text
LANTERN/
│
├── specs/
│   └── cleaned_webgpu.idl
│

├── rules/
│   ├── webgpu_explicit_rules.json
│   └── webgpu_implicit_rules_complete.json
│
├── tools/
│   └── idl_extract_final.py
│   └── copy_runner.sh
│
├── mutator/
│   └── mutator_auto.py
│
├── fuzzer/
│   ├── fuzz4.py
│   └── run_parallel_new.sh
│
├── cts.zip                # Bundled WebGPU CTS (pre-bundled with esbuild; extracted by the user)
├── requirements.txt
└── README.md              # This file  

```

## 1. WebGPU CTS (Baseline Test Corpus)

LANTERN uses the official WebGPU Conformance Test Suite (CTS) as the baseline test corpus. The CTS included in this repository corresponds to a bundled version of the original
WebGPU CTS, prepared to support automated and large-scale execution.

The original CTS consists of multiple subdirectories containing WebGPU test cases, each implemented as a JavaScript specification file (`.spec.js`). Test execution is orchestrated
through the `standalone/index.html` entry point, which invokes the `standalone.js` runner to load and execute individual test cases in a browser environment.

Prior to inclusion in this artifact, all CTS test cases were bundled using *esbuild* to produce self-contained JavaScript files with fully resolved dependencies. The bundling
process replaces the original `.spec.js` files with bundled equivalents, preserving compatibility with the CTS runner while ensuring that each test case executes as a single
JavaScript module. As a result, the CTS archive provided in this repository already contains the bundled test cases and can be executed directly without additional preprocessing.


In addition to the standard browser-based execution mode, LANTERN provides a GUI-free execution pathway for automated testing. A custom runner (`index7.html`) loads
`standalone7.js`, which supports executing predefined CTS queries via a configurable hardcodedQueries. By uncommenting a specific query and loading the corresponding
HTML entry point, individual CTS test cases can be executed deterministically without manual interaction.

### 1.1 Run CTS Locally
The CTS is provided as a compressed archive (cts.zip) to reduce repository size and improve download reliability. Extract it before execution:

```bash
unzip cts.zip
```

```bash
cd cts
python3 -m http.server 8080
```

Open in browser:

```bash
http://localhost:8080/standalone/index.html
```

### 1.2 Run Individual CTS Tests

Individual test cases can be executed by specifying a query parameter. For example:

```bash
http://localhost:8080/standalone/index.html?query=suite=webgpu,group=api,group=adapter,case=requestAdapter_no_parameters

```

### 1.3 GUI-Free Custom Runner

As mentioned above, LANTERN includes a GUI-free runner:

- `index7.html` loads `standalone7.js`

- `standalone7.js` contains a hardcodedQueries list

Uncomment a query in hardcodedQueries list and open:

```bash
http://localhost:8080/standalone/index7.html
```

### 1.4 CTS Bundling Procedure (Already Applied)

The CTS version included in this repository has already been bundled using *esbuild*.
The following command illustrates the procedure used to generate the bundled test cases and is provided for documentation purposes only:

```bash
for file in *.spec.js; do
  esbuild "$file" \
    --bundle \
    --outfile="bundled_${file}" \
    --format=esm \
    --platform=browser \
    --alias:perf_hooks=./perf_hooks_stub.js \
    --inject:./perf_hooks_stub.js
done
```

## 2. Explicit Rule Extraction from WebGPU Specification

LANTERN extracts explicit API constraints directly from the official WebGPU specification. Explicit rules correspond to syntactic and structural constraints that are *explicitly defined* in the specification through WebIDL and describe what is syntactically and structurally allowed by the specification, such as API signatures, parameter types, required fields, and enumerated values. They do not encode semantic assumptions, API ordering, or object state transitions, which are handled separately by implicit rules (Section 3).

### 2.1 IDL Extraction

The script:

```bash
tools/idl_extract_final.py
```

implements a specification-driven extraction pipeline consisting of the
following steps:

- Builds the WebGPU specification locally using Bikeshed

- Extracts all WebIDL blocks from the generated HTML specification

- Cleans, normalizes, and aggregates IDL fragments into a unified IDL file

- Parses the cleaned IDL to derive machine-readable explicit mutation rules

Due to limitations in existing WebIDL parsers (e.g., incomplete support for flag namespaces, mixins, and includes), the extraction process combines AST-based parsing with targeted fallback logic to cover all specification-defined constructs.

The cleaned and aggregated IDL is stored in:

```bash
specs/cleaned_webgpu.idl
```

### 2.2 Generated Explicit Rule Artifacts

From the cleaned WebIDL, LANTERN generates a structured explicit rule set stored in:

```bash
rules/webgpu_explicit_rules.json
```

The extracted explicit rules include:

- Interfaces and method signatures

- Dictionaries and required fields

- Enums and typedefs

- Flag bitmasks with numeric values

- Mixins and `includes`  relationships

These rules define the syntactic and structural validity boundary of WebGPU programs and serve as the foundation for specification-aware mutation.


## 3. Implicit Rule Set (LLM-Inferred)

LANTERN augments explicit rules with implicit semantic constraints, inferred using an LLM and manually validated.

Stored in:

```bash
rules/webgpu_implicit_rules_complete.json
```

These rules include:

- Object state machines (e.g., mapped / unmapped / destroyed)

- Argument constraints (alignment, minimum values)

- Required and forbidden flag combinations

- API call ordering constraints

These implicit rules are not intended to be complete or sound specifications of WebGPU behavior; rather, they encode common constraints extracted from the specification to guide mutation.


## 4. Rule-Guided CTS Mutator

The core mutator is implemented in:

```bash
mutator/mutator_auto.py
```

### 4.1 Mutation Strategy

- Parses JavaScript using Tree-sitter

- Identifies WebGPU API calls

- Applies mutations only when specification-derived rules match

- Tracks whether each mutation comes from an explicit or an implicit rule

### 4.2 Mutation Modes

- **Valid mode**: repairs violations
(e.g., bytesPerRow: 258 → 256)

- **Invalid mode**: introduces violations
(e.g., forbidden flags, incorrect ordering)

### 4.3 Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

git clone https://github.com/tree-sitter/tree-sitter-javascript.git

```
Tree-sitter is used only for parsing and AST traversal; no grammar modifications or instrumentation are required.


### 4.4 Run Mutator (Single Seed)


```bash
source venv/bin/activate

python3 mutator_auto.py \
 --input ./cts \
 --output ./cts_mutated \
 --explicit ./webgpu_explicit_rules.json \
 --implicit ./webgpu_implicit_rules_complete.json \
 --report ./mutation_report.csv \
 --mode invalid \
 --scale 60 \
 --seed 42
  
```


### 4.5 Batch Mutation (Multiple Seeds)

```bash
for i in $(seq 1 50); do
  python3 mutator_auto.py \
    --input ./cts \
    --output ./cts_mutated_60scale$i \
    --explicit ./webgpu_explicit_rules.json \
    --implicit ./webgpu_implicit_rules_complete.json \
    --report ./mutation_report_60scale$i.csv \
    --mode invalid \
    --scale 60 \
    --seed $i
done
```


### 4.6 Post-Mutation Runner Setup

After mutation, copy index7.html from cts/standalone/ into each cts_mutated_*/standalone/ directory to enable GUI-free execution as follows:

```bash
tools/copy_runner.sh
```

## 5. Executing Mutated CTS Tests on Chromium

LANTERN supports three execution modes.

### 5.1 Chromium Requirement

Chromium should be built with ASAN, DCHECK, and debug symbols enabled using the following build configuration:

```bash
is_asan = true
is_debug = true
enable_full_stack_frames_for_profiling = true
symbol_level = 2
dcheck_always_on = true
optimize_debug = false
```


### 5.2 Mode A — Single Mutated Folder Execution

Useful for debugging and deterministic reproduction.

```bash
python fuzzer/fuzz4.py \
  -i ./cts_mutated_7 \
  -o ./fuzz_output_cts_mutated_7 \
  -b "$HOME/chromium/src/out/Default/chrome --enable-unsafe-webgpu --no-sandbox" \
  -p 8000
```

### 5.3 Mode B — Parallel Fuzzing over Multiple Folders

Used for large-scale fuzzing.

```bash
chmod +x fuzzer/run_parallel_new.sh
bash fuzzer/run_parallel_new.sh
```

Behavior:

- Automatically discovers cts_mutated_* folders

- Assigns unique ports

- Launches separate Chromium instances

- Generates independent logs per folder

### 5.4 Mode C — Standalone Chromium Reproduction (No Fuzzer)

Used for manual confirmation and debugging.

```bash
cd cts_mutated_7
python3 -m http.server 8080
```


Then launch Chromium:

```bash
~/chromium/src/out/Default/chrome \
  http://localhost:8080/standalone/index.html \
  --enable-unsafe-webgpu \
  --no-sandbox
```

Optional: run a specific test via URL query.

### 5.5 Recommended Workflow

- Generate mutated CTS folders using the mutator

- Discover crashes using our dynamic conformance testing framework (parallel or single run)

- Reproduce deterministically using single-folder execution or a single query that triggered the crash

- Confirm crashes using pure Chromium 

## 6. Reproducibility Notes

- Mutations are deterministic given the same seed

- All applied mutations are logged along with the rule that triggered them

- Different seeds explore different semantic paths

- Valid and invalid mutation modes are fully separated

- All reproducible security-relevant failures were responsibly disclosed to the Google security team prior to publication, and no exploit code or weaponizable artifacts are included in this release.



## 7. Limitations

- The artifact targets Chromium-based WebGPU implementations and does not evaluate other browser engines.
- Some crashes may be non-deterministic due to GPU scheduling and sandbox interactions.
- The implicit rule set is partial and may not capture all specification constraints.


## Citation

Citation information will be provided upon paper acceptance.


