#!/bin/sh
. .venv/bin/activate
CONFIG_PATH=~/OROC/FlightCardDbs/2026/nxrs.json python -m flight_card_scanner
