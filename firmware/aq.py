# Copyright (c) 2021 RS Components Ltd
# SPDX-License-Identifier: MIT License

'''
Python application for logging data from ESDK hardware
'''

import asyncio
import requests
import websockets
import random
import json
import time
import threading
import logging
import toml
import subprocess
import copy
import geohash
from DesignSpark.ESDK import MAIN, THV, CO2, PM2, AppLogger
import PrometheusWriter, CsvWriter, MQTT

configFile='/boot/aq/aq.toml'
debugEnabled = False

sensorData = {}
debugData = {}

WEBSOCKET_UPDATE_INTERVAL = 5
CSV_UPDATE_INTERVAL = 30
PROMETHEUS_MIN_UPDATE_INTERVAL = 120

HIGH_VOC_SENTENCES = ["The air is smelly", "The world smells bad", "This stinks", "The sky smells bad"]
HIGH_HUMIDITY_SENTENCES = ["It's very moist", "Wet wet wet", "Soggy air", "The air is damp"]
LOW_HUMIDITY_SENTENCES = ["It's very dry", "Dry air"]
HIGH_PM_SENTENCES = ["The air quality is poor", "The air is polluted", "The environment is polluted"]
HIGH_CO2_SENTENCES = ["The air quality is poor", "The air is suffocating"]
NICE_SENTENCES = ["The sky is beautiful", "The air is great", "The world is nice", "The environment is lovely", "The environment is great", "Nature is happy", "The air is fresh"]

async def websocketPush(websocket, path):
    logger.debug("Websocket connection from {}".format(websocket.remote_address))
    while True:
        localData = copy.deepcopy(sensorData)
        localData.update({"debug": debugData})
        await websocket.send(json.dumps(localData, ensure_ascii=False))
        await asyncio.sleep(WEBSOCKET_UPDATE_INTERVAL)

def main():
    global configData
    configData = getConfig()
    debugEnabled = configData['ESDK']['debug']

    # Check for logging level setup, default to full if non-existant configuration
    if 'logging' in configData['ESDK']:
        loggingConfig = configData['ESDK']['logging']
    else:
        loggingConfig = 'full'

    global logger
    logger = AppLogger.getLogger(__name__, debugEnabled, loggingConfig)

    global mainboard
    mainboard = MAIN.ModMAIN(debug=debugEnabled, config=configData, \
        loggingLevel=loggingConfig)

    global appGitCommit
    appGitCommit = {"appVersion": getAppCommitHash()}
    debugData.update(appGitCommit)

    # Give sensor string a restart
    mainboard.setPower(vcc3=False, vcc5=False)
    mainboard.setBuzzer(freq=20000)
    time.sleep(0.5)
    mainboard.setBuzzer(freq=0)
    mainboard.setPower(vcc3=True, vcc5=True)
    
    logger.info("Started main thread")

    hwid = mainboard.getSerialNumber()
    if hwid != -1:
        sensorData.update(hwid)

    debugData.update(mainboard.getModuleVersion())

    # Give sensors some time to sort themselves out before attempting to read
    time.sleep(0.05)
    mainboard.createModules()
    time.sleep(0.05)

    logger.debug("Starting sensor update thread")
    sensorsUpdateThreadHandle = threading.Thread(target=sensorsUpdateThread, \
        args=(sensorData, ), \
        daemon=True)
    sensorsUpdateThreadHandle.name = "sensorsUpdateThread"
    sensorsUpdateThreadHandle.start()

    imageUpdateThreadHandle = threading.Thread(target=imageUpdateThread, \
        args=(sensorData, ), \
        daemon=True)
    imageUpdateThreadHandle.name = "imageUpdateThread"
    imageUpdateThreadHandle.start()

    global mqtt
    mqttConfig = getMqttConfig()

    # MQTT thread init
    if mqttConfig is not None:
        mqtt = MQTT.MQTT(debug=debugEnabled, \
            configDict=getMqttConfig(), \
            hwid=hwid['hardwareId'], \
            loggingLevel=loggingConfig)

        logger.debug("Starting MQTT update thread")
        mqttUpdateThreadHandle = threading.Thread(target=mqttUpdateThread, \
            args=(sensorData, ), \
            daemon=True)

        mqttUpdateThreadHandle.name = "mqttUpdateThread"
        mqttUpdateThreadHandle.start()


    # CSV thread init
    if getCsvEnabled():
        logger.debug("Starting CSV update thread")
        csvUpdateThreadHandle = threading.Thread(target=csvUpdateThread, \
            args=(debugEnabled, sensorData, hwid, loggingConfig), \
            daemon=True)

        csvUpdateThreadHandle.name = "csvUpdateThread"
        csvUpdateThreadHandle.start()


    # Prometheus threads init
    prometheusConfig = getPrometheusConfig()
    if prometheusConfig is not None:
        prometheusThreads = list()

        # Start thread for each configuration present
        for name, config in prometheusConfig.items():
            logger.debug("Starting Prometheus update thread for config {}".format(name))
            prometheusUpdateThreadHandle = threading.Thread(target=prometheusUpdateThread, \
                args=(config, debugEnabled, sensorData, hwid['hardwareId'], loggingConfig), \
                daemon=True)

            prometheusUpdateThreadHandle.name = "prometheusUpdateThread_{}".format(name)
            prometheusThreads.append(prometheusUpdateThreadHandle)
            prometheusUpdateThreadHandle.start()

        logger.debug("Started Prometheus threads: {}".format(prometheusThreads))

    logger.debug("Starting asyncio websocket")
    asyncio.run(startWebsocket())

    while True:
        # Main thread loop
        pass

async def startWebsocket():
    logger.debug("Started websocket")
    async with websockets.serve(websocketPush, "0.0.0.0", 8765):
        await asyncio.Future()  # run forever

def sensorsUpdateThread(sensorDataHandle):
    logger.debug("Started sensor update thread")
    while True:
        sensorDataHandle.update(mainboard.readAllModules())

        try:
            rawLocation = mainboard.getLocation()
            sensorDataHandle.update({'geohash': geohash.encode(rawLocation['lat'], rawLocation['lon'])})
        except Exception as e:
            pass

        debugData.update(mainboard.getUndervoltageStatus())
        debugData.update(mainboard.getGPSStatus())

        time.sleep(1)

def imageUpdateThread(sensorData):
    logger.debug("Start image update thread")
    while True:
        co2 = 0
        voc = 0
        pm = 0
        humidity = 0
        if 'thv' in sensorData:
            voc = sensorData['thv']['vocIndex']
            humidity = sensorData['thv']['humidity']
        if 'pm' in sensorData:
            pm = sensorData['pm']['pm1.0'] + sensorData['pm']['pm2.5'] + sensorData['pm']['pm4.0'] + sensorData['pm']['pm10']
        if 'co2' in sensorData:
            co2 = sensorData['co2']['co2']
        sentences = []
        if voc > 250:
            sentences += HIGH_VOC_SENTENCES
        if humidity > 80:
            sentences += HIGH_HUMIDITY_SENTENCES
        if humidity < 20 and humidity != 0:
            sentences += LOW_HUMIDITY_SENTENCES
        if pm > 100:
            sentences += HIGH_PM_SENTENCES
        if co2 > 2000:
            sentences += HIGH_CO2_SENTENCES
        if len(sentences) == 0:
            sentences += NICE_SENTENCES
        sentence = random.choice(sentences)
        try:
            requests.get("http://airquality.mikeasoft.com/%s" % sentence)
        except:
            time.sleep(120)
            requests.get("http://airquality.mikeasoft.com/%s" % sentence)
        time.sleep(3600)

def mqttUpdateThread(sensorData):
    logger.debug("Started MQTT thread")
    while True:
        mqtt.publishMessage(json.dumps(sensorData))
        time.sleep(WEBSOCKET_UPDATE_INTERVAL)

def prometheusUpdateThread(config, debugEnabled, sensorData, hwid, loggingLevel):
    logger.debug("Started Prometheus update thread")

    localConfig = config
    localConfig.update({'friendlyname': getFriendlyName()})

    # Enforce a mininmum logging interval
    if int(localConfig['interval']) < PROMETHEUS_MIN_UPDATE_INTERVAL:
        localConfig['interval'] = PROMETHEUS_MIN_UPDATE_INTERVAL

    writer = PrometheusWriter.PrometheusWriter(configDict=localConfig, \
        debug=debugEnabled, \
        hwid=hwid, \
        loggingLevel=loggingLevel, \
        additionalLabels=configData['ESDK']) # 'ESDK' dictionary should contain any additional labels
                                             # pulled from the config file
    logger.debug("Update interval {}s".format(localConfig['interval']))

    while True:
        writer.writeData(sensorData)
        time.sleep(localConfig['interval'])

def csvUpdateThread(debugEnabled, sensorData, hwid, loggingLevel):
    logger.debug("Started CSV update thread")
    csv = CsvWriter.CsvWriter(debug=debugEnabled, \
        friendlyName=getFriendlyName(), \
        hwid=hwid['hardwareId'], \
        loggingLevel=loggingLevel)
    while True:
        csv.addRow(sensorData)
        time.sleep(CSV_UPDATE_INTERVAL)

def getConfig():
    """ Open configuration file to read config information, then close """
    with open(configFile) as fh:
        return toml.loads(fh.read())

def getMqttConfig():
    """ Return a dictionary containing MQTT config """
    if 'mqtt' in configData:
        return configData['mqtt']
    else:
        return None

def getPrometheusConfig():
    """ Return a dictionary containing Prometheus config, and friendly name """
    if 'prometheus' in configData:
        return(configData['prometheus'])
    else:
        return None

def getFriendlyName():
    """ Return the string of the device friendly name """
    return configData['ESDK']['friendlyname']

def getCsvEnabled():
    """ Return CSV enabled value """
    if configData['local']['csv'] is not None:
        return configData['local']['csv']
    else:
        return False

def getAppCommitHash() -> str:
    """ Try get application git commit hash """
    # Taken from https://stackoverflow.com/a/21901260
    try:
        return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode('ascii').strip()
    except Exception as e:
        logger.error("Could not determine Git hash! Reason {}".format(e))
        return ""

if __name__ == "__main__":
    main()
