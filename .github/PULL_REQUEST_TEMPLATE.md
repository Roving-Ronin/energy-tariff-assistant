<!--- Provide a general summary of your changes in the Title above. -->

## Description

<!--- Describe your changes in detail. -->
<!--- If the change is breaking, detail what breaks and what users need to do to fix it. -->

## Area(s) affected

- [ ] Setup / options flow (`config_flow.py`, `plan_source.py`, `strings.json`)
- [ ] Rate engine (`engine/resolver.py`, `engine/forecast.py`, `engine/model.py`)
- [ ] Metering (`engine/meter.py`)
- [ ] Plan format / validation (`engine/validate.py`, `schema/`)
- [ ] Sensors / runtime (`sensor.py`, `runtime.py`)
- [ ] Plan library (`plans/`, `scripts/`)
- [ ] Repo only (README, workflows, tests)

## Related issues

<!--- If suggesting a new feature or change, please discuss it in an issue first. -->
<!--- If fixing a bug, there should be an issue describing it with steps to reproduce. -->
<!--- Link the issue here, e.g. fixes #123 -->

## Motivation and Context

<!--- Why is this change required? What problem does it solve? -->

## How has this been tested

<!--- Describe how you tested your changes, e.g. which plan, which energy sensors, which times of day. -->

- Home Assistant version:
- Plan / energy sensors:

## Type of change

Please delete options that are not relevant.

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing configurations to not work as expected)
- [ ] Documentation update

## Checklist

<!--- Put an `x` in all the boxes that apply. -->

- [ ] I have performed a self-review of my code
- [ ] I have commented my code, particularly in hard-to-understand areas
- [ ] `python -m pytest -q` passes, and I have added tests for new behaviour
- [ ] I have updated `strings.json` and copied it to `translations/en.json` for any UI text changes
- [ ] My changes generate no new errors or warnings in the Home Assistant log
- [ ] I have updated the README if setup, sensors or behaviour changed
- [ ] For plan files: `python scripts/validate_plans.py` and `python scripts/build_index.py` pass
