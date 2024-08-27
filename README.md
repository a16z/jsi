# jsi

just solve it - a CLI utility to run a portfolio of SMT solvers in parallel

## Getting Started

```sh
# install jsi
uv tool install jsi

# run it
jsi --help
```

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
```

## Acknowledgements

The setup for this project is based on [postmodern-python](https://rdrn.me/postmodern-python/).
