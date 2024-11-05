# jsi

just solve it - a command-line utility to run a portfolio of [SMT](https://en.wikipedia.org/wiki/Satisfiability_modulo_theories) solvers in parallel.

![Screenshot of jsi running on an unsat division problem](static/images/unsat-div-screenshot.png)


## Highlights

- 🏆 acts as a "virtual best solver" by running multiple solvers in parallel and returning the result of the fastest solver
- 🔍 discovers available solvers on on the PATH at runtime
- 🛣️ runs solvers in parallel and monitors their progress
- ⏰ can terminate solvers early after a timeout
- 🔪 jsi can be interrupted by Ctrl-C and it will kill any solvers still running
- 🏎 runs with minimal startup time (<100ms), and also supports an experimental daemon mode with a rust client for extra low-latency (<10ms)
- 🖥️ supports macOS and Linux


## Getting Started

We recommend using [uv](https://docs.astral.sh/uv/) to install jsi.

```sh
# install jsi
uv tool install jsi

# run it
jsi --help
```


## Features

### Configuration

This is how jsi finds and runs solvers:

- it first attempts to load custom solver definitions from `~/.jsi/solvers.json`
- if that file doesn't exist, it loads the default definitions from the installed package (see [src/jsi/config/solvers.json](src/jsi/config/solvers.json))

Based on these definitions, jsi knows what executables to look for, whether a given solver is enabled, how to enable model generation, etc.

Then:
- it looks up the solver cache in `~/.jsi/cache.json`
- if that file doesn't exist, it will scan the PATH and cache the results

It does this because scanning the PATH can be slow, but loading cached paths is 5x faster.

> [!TIP]
> `~/.jsi/cache.json` can always be safely deleted, jsi will generate it again next time it runs. If you make changes to `~/.jsi/solvers.json` (like adding a new solver), you should delete the cache file, otherwise jsi won't pick up the new solver.


## Development

Pre-requisites: install [rye](https://rye.astral.sh/guide/installation/#installing-rye)

```sh
# clone the repo
git clone https://github.com/a16z/jsi
cd jsi

# install dependencies
rye sync

# runs the formatter, linter, type checker and tests
rye run all

# run the tool
rye run jsi --help

# run it without rye
python -m jsi --help

# run a single test
uv run pytest -v -k <test_name>
```

### Redirect log output

```sh
# jsi will print its own output to stderr, so you can redirect it to a file
# (stdout is reserved for the best solver's output)
jsi 2> jsi.logs
```

## Acknowledgements

The setup for this project is based on [postmodern-python](https://rdrn.me/postmodern-python/).

## Disclaimer

_This code is being provided as is. No guarantee, representation or warranty is being made, express or implied, as to the safety or correctness of the code. It has not been audited and as such there can be no assurance it will work as intended, and users may experience delays, failures, errors, omissions or loss of transmitted information. Nothing in this repo should be construed as investment advice or legal advice for any particular facts or circumstances and is not meant to replace competent counsel. It is strongly advised for you to contact a reputable attorney in your jurisdiction for any questions or concerns with respect thereto. a16z is not liable for any use of the foregoing, and users should proceed with caution and use at their own risk. See a16z.com/disclosures for more info._
