# Copyright (C) 2026 FMA ALPHA QUANT LABS
# AGPL-3.0 — see LICENSE file for more details.

"""engine/ — modul yang dipotong dari main.py: state runtime, position state
machine, dan siklus portfolio. Semuanya akses state lewat engine.state, tidak
ada yang mengimpor main.py (circular).
"""
