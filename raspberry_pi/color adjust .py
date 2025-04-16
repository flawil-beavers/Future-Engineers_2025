import asyncio
import base64
import json
import math
import os
from time import sleep
import cv2
import numpy as np
import serial
import serial.tools.list_ports
from websockets import WebSocketServerProtocol, serve
from config import ConfigLoader
from helpers import Pillar, extract_ROI
from pipeline import Pipeline
from statemachine import StateMachine
from picamera2 import Picamera2
from rounddir import find_round_dir
import argparse
import libcamera

configloader = ConfigLoader("config.json")
pipeline = Pipeline(configloader)

picam2 = Picamera2()
# picam2.configure(picam2.create_preview_configuration())
preview_config = picam2.create_preview_configuration()
preview_config["transform"] = libcamera.Transform(vflip=True, hflip=True)
picam2.configure(preview_config)
picam2.start()

picam2.set_controls({
    "AwbEnable": False,
    # controls.AWB_TEMPERATURE: fixed_temperature
})


ports = serial.tools.list_ports.comports()


try:
  ser = serial.Serial(configloader.get_property("ArduinoSerialPort"), 9600)
except:
  print("Arduino not connected, available devices")
  ser = None
  for port, desc, hwid in sorted(ports):
        print("{}: {} [{}]".format(port, desc, hwid))