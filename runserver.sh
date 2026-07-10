#!/bin/sh
. .venv/bin/activate
CONFIG_PATH=~/OROC/FlightCardDbs/2026/nxrs/config.json python -m flight_card_scanner
