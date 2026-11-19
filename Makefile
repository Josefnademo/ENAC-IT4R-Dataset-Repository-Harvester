# ENAC-IT4R Dataset & Repository Harvester - developer shortcuts.
# Every target also runs without make: python tasks.py <target>

PYTHON ?= python

.PHONY: run test check harvest clean

run:
	$(PYTHON) run.py

test:
	$(PYTHON) -m unittest discover -s tests -v

check:
	$(PYTHON) -m compileall -q backend run.py tests
	node --check frontend/preview.js && node --check frontend/app.js
	$(PYTHON) -m unittest discover -s tests -v

harvest:
	$(PYTHON) run.py --harvest --days 7 --type dataset

clean:
	$(PYTHON) tasks.py clean
