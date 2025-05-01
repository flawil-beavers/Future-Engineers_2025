#!/bin/bash
cp car.service /lib/systemd/system/
systemctl daemon-reload
systemctl enable car.service