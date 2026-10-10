# Vendored tracking number data

- Upstream: https://github.com/jkeen/tracking_number_data
- Commit: 03af203a4eb34981fa79b9670b20ab7945a96f2d
- License: MIT (see LICENSE)
- Vendored: `couriers/*.json` and `LICENSE`, unmodified.
- Rationale: vendor the JSON rather than the stale 2022 PyPI port.
- Regex note: `regex` is a list of fragments to join; `(?<name>` must be converted to `(?P<name>` for Python `re`.
