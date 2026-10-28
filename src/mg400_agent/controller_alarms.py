# ruff: noqa: E501 - Vendor alarm text stays verbatim.
"""MG400 alarm IDs from GetErrorID(), with Dobot's English descriptions and solutions.

The tables are a subset of alarm_controller.json and alarm_servo.json from
https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/tree/b5f95647cfc44a60f92babf5a464d38dd1e4f72c/files
They keep only the English description and solution. Dobot licenses them under MIT:

    Copyright (c) 2022 Dobot

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""

import json

CONTROLLER_ALARMS = {
    1: ("UniIO Borad dropped", ""),
    2: ("Axis 1 dropped", ""),
    3: ("Axis 2 dropped", ""),
    4: ("Axis 3 dropped", ""),
    5: ("Axis 4 dropped", ""),
    16: ("Inverse kinematics error with singularity", "Reselect movement points"),
    17: ("Inverse kinematics error with no solution", "Reselect movement points"),
    18: ("Target position triggers joint limit", "Reselect movement points"),
    19: ("Duplicated data in JUMP or ARC or Circles instruction", "Reselect movement points"),
    20: ("Wrong input parameters for arc", "Enter the correct parameters"),
    21: (
        "The Start and the End is negative or the zLimit is below the start and end points",
        "Enter the correct parameters",
    ),
    22: ("Wrong arm orientation switch", "Reselect movement points"),
    23: ("Plan point during linear motion out of working area", "Reselect movement points"),
    24: ("Plan point during circular arc motion out of working area", "Reselect movement points"),
    25: (
        "Wrong mode for motion instruction",
        "Internal software error, restart or contact manufacturer",
    ),
    26: ("Wrong input parameters for speed", "input correct parameter"),
    27: ("Wrong trajectory motion plan of continuous path", "input correct parameter"),
    28: ("Wrong input parameters for circle", "input correct parameter"),
    29: (
        "Plan point during circular circle motion out of working circle",
        "Reselect movement points",
    ),
    30: ("Inching target position inaccessible", "Reverse inch out of limit"),
    32: ("Inverse kinematics singularity during moving", "Reselect movement points"),
    33: ("Inverse kinematics with no solution during moving", "Reselect movement points"),
    34: ("Inverse kinematcis with result out of working area", "Reselect movement points"),
    48: (
        "Joint1 overspeed",
        "Reset the speed or re-select the movement point away from the singularity",
    ),
    49: (
        "Joint2 overspeed",
        "Reset the speed or re-select the movement point away from the singularity",
    ),
    50: (
        "Joint3 overspeed",
        "Reset the speed or re-select the movement point away from the singularity",
    ),
    51: (
        "Joint4 overspeed",
        "Reset the speed or re-select the movement point away from the singularity",
    ),
    52: ("Joint1 position out of range", "Internal error, restart or contact manufacturer"),
    53: ("Joint2 position lag error", "Internal error, restart or contact manufacturer"),
    54: ("Joint3 position lag error", "Internal error, restart or contact manufacturer"),
    55: ("Joint4 position lag error", "Internal error, restart or contact manufacturer"),
    64: ("Joint1 exceeds positive limit", "Reverse jog out of limit"),
    65: ("Joint1 exceeds negative limit", "Reverse jog out of limit"),
    66: ("Joint2 exceeds positive limit", "Reverse jog out of limit"),
    67: ("Joint2 exceeds negative limit", "Reverse jog out of limit"),
    68: ("Joint3 exceeds positive limit", "Reverse jog out of limit"),
    69: ("Joint3 exceeds negative limit", "Reverse jog out of limit"),
    70: ("Joint4 exceeds positive limit", "Reverse jog out of limit"),
    71: ("Joint4 exceeds negative limit", "Reverse jog out of limit"),
    72: ("Parallelogram positive limit", "Reverse jog out of limit"),
    73: ("Parallelogram negative limit", "Reverse jog out of limit"),
    74: ("Joint6 exceeds positive limit", ""),
    75: ("Joint6 exceeds negative limit", ""),
    80: ("Joint1 lose step", "Internal error, restart or contact manufacturer"),
    81: ("Joint2 lose step", "Internal error, restart or contact manufacturer"),
    82: ("Joint3 lose step", "Internal error, restart or contact manufacturer"),
    83: ("Joint4 lose step", "Internal error, restart or contact manufacturer"),
    84: ("Algorithm timeout", "Internal error, restart or contact manufacturer"),
    85: (
        "Hardware emergency stop button press or software emergency stop trigger",
        "If the hardware emergency stop button is pressed, release the hardware emergency stop button; if the software emergency stop triggers, click the software emergency stop button again",
    ),
    86: ("User emergency button pressed", "Release the user emergency stop button"),
    96: (
        "Joint1 drive alarm",
        "Check if the communication of joint 1 is normal and then clear the error",
    ),
    97: ("Joint1 Servo power off", "Re-enable joint 1"),
    98: (
        "Joint2 drive alarm",
        "Check if the communication of joint 2 is normal and then clear the error",
    ),
    99: ("Joint2 Servo power off", "Re-enable joint 2"),
    100: ("Joint3 drive alarm", "Re-enable joint 3"),
    101: ("Joint3 Servo power off", "Re-enable joint 3"),
    102: ("Joint4 drive alarm", "Re-enable joint 4"),
    103: ("Joint4 drive power off", "Re-enable joint 4"),
    104: ("Robot homing failed", "Home again"),
    105: ("Robot Servo on failed", "Check whether the hardware is normal and re-enable"),
    106: ("Abnormal conveyor data", ""),
    107: ("Abnormal conveyor synchronization", ""),
    108: ("Conveyor conveyor encoder 1 is disconnected", ""),
    109: ("Conveyor conveyor encoder 2 is disconnected", ""),
    110: ("Encoder position error", "Internal error, restart or contact manufacturer"),
    112: ("Collision Detection", "Keep away from the work area and continue to run"),
    161: ("Error switching drag and drop mode", "Internal error, restart or contact manufacturer"),
    4096: ("Faield to open mechanical file", "Check if the file location is correct and restart"),
    8192: ("Faield to open project file", "Check if the file location is correct and restart"),
    8193: ("Faield to open program file", "Check if the file location is correct and restart"),
    8194: (
        "Faield to open global variable file",
        "Check if the file location is correct and restart",
    ),
    8195: (
        "Failed to open teaching point file",
        "Check if the file location is correct and restart",
    ),
    8196: ("Failed to start debugger process", "Rerun"),
    12288: ("Emergency stop detected", "Power on again"),
    12289: ("External emergency stop detected", "Power on again"),
    12290: (
        "The servo power board temperature is too high",
        "Turn off the machine and let it cool for a period of time",
    ),
    12292: ("Safeguard stop detected", "After ensuring safety, restore the safeguard device"),
    12293: ("servor offline detected", "Internal error, restart or contact manufacturer"),
    12294: ("Collision Detection", "Keep away from the work area and continue to run"),
    20480: ("AUX Joint1 exceeds positive limit", "Reverse jog out of limit"),
    20481: ("AUX Joint1 exceeds negative limit", "Reverse jog out of limit"),
    20482: (
        "AUX Joint1 communication failed",
        "Check if the communication of AUX joint 1 is normal and then clear the error",
    ),
    20483: ("AUX Joint1 position out of working area", "Reselect movement points"),
    20484: ("AUX Joint1 emergency stop button press", "release the hardware emergency stop button"),
    32768: ("No input parameters for speedFactor instruction", "Enter the correct parameters"),
    32769: (
        "Input parameters of speedFactor instruction out of range",
        "Enter the correct parameters",
    ),
    32770: ("DO input parameters Error", "Enter the correct parameters"),
    32771: ("DI input parameters Error", "Enter the correct parameters"),
    32776: ("DOGroup params number err", "params is too much"),
    32777: ("DOGroup have some params's index out of range", "input correct params"),
    32778: ("DOGroup have some params's index type error", "input correct params"),
    32779: (
        "DOGroup have some params's value type error",
        "input correct params,number type and boolean is needed eg.(0/1) || (true/false) || (ON/OFF)",
    ),
    32780: (
        "DOGroup have some params's isqueue type error",
        "eg.(0/1) || (true/false) || (ON/OFF) . true means queue command(default),false:Immediate command",
    ),
    32781: ("DOGroup err internal", "internal error has happend,try again."),
    33024: ("No input parameters for CP instruction", "Enter the correct parameters"),
    33025: ("Input parameters of CP instruction out of range", "Enter the correct parameters"),
    33280: ("No input parameters for Arch instruction", "Please enter parameters"),
    33281: ("Index parameter of Arch instruction out of range", "Enter the correct parameters"),
    33282: (
        "Index parameter of Arch instruction not configured yet",
        "Please set index parameters",
    ),
    33536: ("No input parameters for LimZ instruction", "Please enter parameters"),
    33537: ("Input parameters of LimZ instruction out of range", "Enter the correct parameters"),
    33792: ("No input parameters for Speed instruction", "Please enter parameters"),
    33793: (
        "Ratio parameter of Speed instruction out of range [1, 100]",
        "Enter the correct parameters",
    ),
    34048: ("No input parameters for Accel instruction", "Please enter parameters"),
    34049: (
        "Ratio parameter of Accel instruction out of range [1, 100]",
        "Enter the correct parameters",
    ),
    34304: ("No input parameters for Jerk instruction", "Please enter parameters"),
    34305: (
        "Ratio parameter of Jerk instruction out of range [1, 100]",
        "Enter the correct parameters",
    ),
    34560: ("No input parameters for SpeedS instruction", "Please enter parameters"),
    34561: (
        "Ratio parameter of SpeedS instruction out of range [1, 100]",
        "Enter the correct parameters",
    ),
    34816: ("No input parameters for SpeedR instruction", "Please enter parameters"),
    34817: (
        "Ratio parameter of SpeedR instruction out of range [1, 100]",
        "Enter the correct parameters",
    ),
    35072: ("No input parameters for AccelS instruction", "Please enter parameters"),
    35073: (
        "Ratio parameter of AccelS instruction out of range [1, 100]",
        "Enter the correct parameters",
    ),
    35328: ("No input parameters for AccelR instruction", "Please enter parameters"),
    35329: (
        "Ratio parameter of AccelR instruction out of range [1, 100]",
        "Enter the correct parameters",
    ),
    35584: ("No input parameters for JerkS instruction", "Please enter parameters"),
    35585: (
        "Ratio parameter of JerkS instruction out of range [1, 100]",
        "Enter the correct parameters",
    ),
    35840: ("No input parameters for JerkR instruction", "Please enter parameters"),
    35841: (
        "Ratio parameter of JerkR instruction out of range [1, 100]",
        "Enter the correct parameters",
    ),
    36096: ("No input parameters for Go instruction", "Please enter parameters"),
    36097: ("No motion point parameter for Go instruction", "Please enter parameters"),
    36098: ("Incorrect motion point for Go instruction", "Enter the correct parameters"),
    36099: ("Incorrect control parameter for Go instruction", "Enter the correct parameters"),
    36100: ("No input parameters for movej instruction", "Enter the correct parameters"),
    36101: ("No motion point parameter for movej instruction", "Enter the correct parameters"),
    36102: ("No motion point parameter for movej instruction", "Enter the correct parameters"),
    36103: ("Incorrect motion point for RP instruction", "Enter the correct parameters"),
    36104: ("Incorrect offset for RP instruction", "Enter the correct parameters"),
    36105: ("Incorrect motion point for RJ instruction", "Enter the correct parameters"),
    36106: ("Incorrect offset for RJ instruction", "Enter the correct parameters"),
    36107: ("No input parameters for GoR instruction", "Enter the correct parameters"),
    36108: ("Incorrect motion point for GoR instruction", "Enter the correct parameters"),
    36109: ("No input parameters for MoveJR instruction", "Enter the correct parameters"),
    36110: ("Incorrect motion point for MoveJR instruction", "Enter the correct parameters"),
    36111: ("No input parameters for GoIO instruction", "Enter the correct parameters"),
    36112: ("Incorrect motion point for GoIO instruction", "Enter the correct parameters"),
    36113: ("Incorrect parameters for GoIO instruction", "Enter the correct parameters"),
    36114: ("No input parameters for MoveIO instruction", "Enter the correct parameters"),
    36115: ("Incorrect motion point for MoveIO instruction", "Enter the correct parameters"),
    36116: ("Incorrect parameters for MoveIO instruction", "Enter the correct parameters"),
    36117: ("No input parameters for MoveJIO instruction", "Enter the correct parameters"),
    36118: ("Incorrect motion point for MoveJIO instruction", "Enter the correct parameters"),
    36119: ("No input parameters for MoveJIO instruction", "Enter the correct parameters"),
    36121: ("RP Inverse kinematics error with no solution", "Reselect movement points"),
    36122: (
        "RP Inverse kinematics error with result out of working area",
        "Reselect movement points",
    ),
    36352: ("No input parameters for Move instruction", "Please enter parameters"),
    36353: ("No motion point parameter for Move instruction", "Please enter parameters"),
    36354: ("Incorrect motion point for Move instruction", "Enter the correct parameters"),
    36355: ("Incorrect control parameter for Move instruction", "Enter the correct parameters"),
    36608: ("No input parameters for Arch3 instruction", "Please enter parameters"),
    36609: ("No motion point parameter for Arch3 instruction", "Please enter parameters"),
    36610: ("Incorrect motion point for Arch3 instruction", "Enter the correct parameters"),
    36611: ("Incorrect control parameter for Arch3 instruction", "Enter the correct parameters"),
    36864: ("No input parameters for Jump instruction", "Please enter parameters"),
    36865: ("No motion point parameter for Jump instruction", "Please enter parameters"),
    36866: ("Incorrect motion point for Jump instruction", "Enter the correct parameters"),
    36867: ("Incorrect control parameter for Jump instruction", "Enter the correct parameters"),
    40960: ("No input parameters for Circle3 instruction", "Please enter parameters"),
    40961: ("No motion point parameter for Circle3 instruction", "Please enter parameters"),
    40962: ("Incorrect motion point for Circle3 instruction", "Enter the correct parameters"),
    40963: ("Incorrect control parameter for Circle3 instruction", "Enter the correct parameters"),
    45056: ("Circle3 Option Error", "Enter the correct parameters"),
    45057: ("Jump Option Error", "Enter the correct parameters"),
    45058: ("Arch Option Error", "Enter the correct parameters"),
    45059: ("Arch3 Option Error", "Enter the correct parameters"),
    45060: ("Jerk Option Error", "Enter the correct parameters"),
    45061: ("JerkR Option Error", "Enter the correct parameters"),
    45062: ("JerkS Option Error", "Enter the correct parameters"),
    45063: ("Accel Option Error", "Enter the correct parameters"),
    45064: ("AccelR Option Error", "Enter the correct parameters"),
    45065: ("AccelS Option Error", "Enter the correct parameters"),
    45066: ("SpeedFactor Option Error", "Enter the correct parameters"),
    45067: ("Speed Option Error", "Enter the correct parameters"),
    45068: ("SpeedR Option Error", "Enter the correct parameters"),
    45069: ("Limz Option Error", "Enter the correct parameters"),
    45070: ("CP Option Error", "Enter the correct parameters"),
    45071: ("DO Option Error", "Enter the correct parameters"),
    45072: ("Go Option Error", "Enter the correct parameters"),
    45073: ("Move Option Error", "Enter the correct parameters"),
    45074: ("MoveJ Option Error", "Enter the correct parameters"),
    45075: ("Ecp Option Error", "Enter the correct parameters"),
    45076: ("EcpSet Option Error", "Enter the correct parameters"),
    45077: ("SetExicitMode Option Error", "Enter the correct parameters"),
    45079: ("loadSwitch Option Error", "Enter the correct parameters"),
    45080: ("loadSet Options Error", "Enter the correct parameters"),
    45081: ("CPParamErrorOption", "Enter the correct parameters"),
    45082: ("TOOLParamErrorOption", "Enter the correct parameters"),
    45083: ("USERParamErrorOption", "Enter the correct parameters"),
    45084: ("SPEEDParamErrorOption", "Enter the correct parameters"),
    45085: ("SPEEDSParamErrorOption", "Enter the correct parameters"),
    45086: ("ACCELParamErrorOption", "Enter the correct parameters"),
    45087: ("ACCELSParamErrorOption", "Enter the correct parameters"),
    45088: ("ARCHParamErrorOption", "Enter the correct parameters"),
    45089: ("STARTParamErrorOption", "Enter the correct parameters"),
    45090: ("ZLIMITParamErrorOption", "Enter the correct parameters"),
    45091: ("ENDParamErrorOption", "Enter the correct parameters"),
    45092: ("SYNCaramErrorOption", "Enter the correct parameters"),
    45093: ("ARMParamErrorOption", "Enter the correct parameters"),
    45312: ("loadSwitch Option Error", "Enter the correct parameters"),
    45313: ("loadSet Options Error", "Enter the correct parameters"),
    49152: ("Enable remote control when enabled", "Next enable and then switch"),
}

SERVO_ALARMS = {
    8752: ("Hardware overcurrent", "System error, please contact technical support engineer"),
    8977: (
        "DQ axis current overflow fault",
        "System error, please contact technical support engineer",
    ),
    8980: ("Busbar overcurrent", "System error, please contact technical support engineer"),
    8992: ("Software overcurrent", "System error, please contact technical support engineer"),
    9024: ("Output shorted to ground", "Please contact technical support engineer"),
    9040: ("Drive overload", "Replace the driver"),
    9088: (
        "Current zero point too large",
        "System error, please contact technical support engineer",
    ),
    12576: (
        "Control of electrical undervoltage",
        "System error, please contact technical support engineer",
    ),
    12592: (
        "Main circuit electrical shortage",
        "Check the cable connection of power, otherwise, replace the driver",
    ),
    12597: (
        "Input phase failure warning",
        "System error, please contact technical support engineer",
    ),
    12816: (
        "Electrical over-voltage in the main circuit",
        "System error, please contact technical support engineer",
    ),
    12817: (
        "Brake resistor overload alarm",
        "System error, please contact technical support engineer",
    ),
    12832: (
        "Main circuit voltage undervoltage",
        "System error, please contact technical support engineer",
    ),
    12851: ("Motor overload warning", "System error, please contact technical support engineer"),
    12928: (
        "Servo and motor power matching faults",
        "System error, please contact technical support engineer",
    ),
    13105: (
        "Motor power cable disconnection",
        "System error, please contact technical support engineer",
    ),
    13184: ("UVW phase sequence error", "System error, please contact technical support engineer"),
    17168: (
        "Radiator overheating",
        "Drop the environment temperature, or contact technical support engineer",
    ),
    17169: (
        "Damaged or uninstalled temperature measuring resistors",
        "System error, please contact technical support engineer",
    ),
    21120: (
        "Programmable logic configuration faults",
        "System error, please contact technical support engineer",
    ),
    21121: ("internal error", "System error, please contact technical support engineer"),
    21569: (
        "Upper and lower board connection failure",
        "System error, please contact technical support engineer",
    ),
    21570: ("DI emergency brake", "System error, please contact technical support engineer"),
    21571: (
        "Forward overtravel warning",
        "System error, please contact technical support engineer",
    ),
    21572: (
        "Reverse overtravel warning",
        "System error, please contact technical support engineer",
    ),
    21574: (
        "Invalid servo ON command fault",
        "System error, please contact technical support engineer",
    ),
    21808: (
        "Parameter storage failure",
        "Reset the parameter and power on again, or please contact technical support engineer",
    ),
    25376: (
        "Abnormalities in internal servo parameters",
        "System error, please contact technical support engineer",
    ),
    25377: (
        "Internal program exceptions",
        "System error, please contact technical support engineer",
    ),
    25378: (
        "Repeated assignment of DI functions",
        "1. Check whether the same function is assigned to different DI's; 2. Confirm whether the corresponding MCU supports the assigned functionality",
    ),
    25379: (
        "DO function allocation overrun",
        "Check whether the motor and circuit are working properly, or contact technical support engineer",
    ),
    25380: (
        "Electronic gear setting overrun",
        "System error, please contact technical support engineer",
    ),
    25381: (
        "Wrong parameter setting for fully closed loop function",
        "System error, please contact technical support engineer",
    ),
    25382: (
        "Software position upper and lower limits set incorrectly",
        "System error, please contact technical support engineer",
    ),
    25383: (
        "Wrong home position offset setting",
        "System error, please contact technical support engineer",
    ),
    25384: (
        "Fault in crossover pulse output setting",
        "System error, please contact technical support engineer",
    ),
    25385: (
        "External braking resistor too small",
        "System error, please contact technical support engineer",
    ),
    25386: (
        "Change of parameters requires re-powering to take effect",
        "Clear the alarm and power on again",
    ),
    28961: (
        "Overheating protection for blocked motors",
        "Check whether the hardware is working properly, or contact technical support engineer",
    ),
    28962: (
        "Product matching faults",
        "1. Check whether the motor parameter matches the motor model in nameplate; 2.Check whether the motor and driver match, otherwise, select the right motor and driver",
    ),
    28964: (
        "Absolute position mode product matching fault",
        "System error, please contact technical support engineer",
    ),
    29056: ("Motor overload", "System error, please contact technical support engineer"),
    29488: (
        "Data in the motor encoder ROM is incorrectly checked or parameters are not stored",
        "系统错误，请联系技术支持工程师",
    ),
    29490: ("Encoder multi-turn count error", "Replace the motor"),
    29491: (
        "Encoder multi-turn count overflow",
        "System error, please contact technical support engineer",
    ),
    29492: ("Encoder interference", "System error, please contact technical support engineer"),
    29493: (
        "External encoder scale failure",
        "System error, please contact technical support engineer",
    ),
    29494: (
        "Encoder data abnormalities",
        "System error, please contact technical support engineer",
    ),
    29495: (
        "Encoder return checksum exception",
        "System error, please contact technical support engineer",
    ),
    29496: ("Loss of encoder Z signal", "System error, please contact technical support engineer"),
    29568: (
        "Encoder connection error",
        "Check the cable connection of encoder, or contact technical support engineer",
    ),
    29569: (
        "Internal failure of the encoder",
        "System error, please contact technical support engineer",
    ),
    29570: ("Encoder battery warning", "Replace battery"),
    29571: ("Encoder battery failure", "Connect battery, or contact technical support engineer"),
    29572: (
        "Encoder Eeprom reading CRC fault",
        "System error, please contact technical support engineer",
    ),
    29953: ("FPGA software version too low", "Please contact technical support engineer"),
    29954: (
        "Programmable logic interrupt fault",
        "If connecting the power for many times, the alarm is still reported, please replace the drive",
    ),
    29956: ("FPGA configuration error", "System error, please contact technical support engineer"),
    30080: (
        "EtherCAT communication failure",
        "System error, please contact technical support engineer",
    ),
    30081: ("Unburned XML configuration file", "Burn the XML configuration file"),
    30082: (
        "Sync cycle configuration error",
        "System error, please contact technical support engineer",
    ),
    30083: ("Loss of synchronisation", ""),
    30084: (
        "Excessive synchronisation period error",
        "System error, please contact technical support engineer",
    ),
    30208: (
        "Frequent parameter storage",
        "Check whether the upper computer is working normal, or contact technical support engineer",
    ),
    33920: ("Overspeed", "System error, please contact technical support engineer"),
    33921: (
        "Excessive speed tracking error",
        "System error, please contact technical support engineer",
    ),
    33922: ("Flying Cars", "Please contact technical support engineer"),
    34321: (
        "Excessive position deviation",
        "Check whether the motor is working properly, or contact technical support engineer",
    ),
    34322: (
        "Position command too large",
        "System error, please contact technical support engineer",
    ),
    34323: (
        "Excessive deviation from fully closed-loop position",
        "System error, please contact technical support engineer",
    ),
    51020: (
        "Driver board identification error",
        "System error, please contact technical support engineer",
    ),
    65282: (
        "Failure to identify angles",
        "System error, please contact technical support engineer",
    ),
    65288: (
        "FPGA system sampling operation timeout",
        "System error, please contact technical support engineer",
    ),
    65296: ("Pulse output overspeed", "System error, please contact technical support engineer"),
    65298: (
        "Network initialization failure",
        "System error, please contact technical support engineer",
    ),
    65344: (
        "Parameter recognition failure",
        "System error, please contact technical support engineer",
    ),
    65432: (
        "Zero return mode setting error",
        "System error, please contact technical support engineer",
    ),
    65521: ("Zero return timeout fault", "System error, please contact technical support engineer"),
}


def parse_alarm_ids(payload):
    """Parse the GetErrorID() payload into one tuple of integer IDs per list."""
    try:
        groups = json.loads(payload)
    except ValueError as error:
        raise RuntimeError(f"Invalid GetErrorID() payload: {payload}") from error
    if (
        not isinstance(groups, list)
        or len(groups) < 5
        or not all(
            isinstance(group, list) and all(type(item) is int for item in group) for group in groups
        )
    ):
        raise RuntimeError(f"Invalid GetErrorID() payload: {payload}")
    return tuple(tuple(group) for group in groups)


def describe_alarms(groups):
    """Name each alarm. The last four lists are the J1-J4 servos; the rest are controller lists."""
    alarms = []
    servos = len(groups) - 4
    for index, group in enumerate(groups):
        source = "controller" if index < servos else f"J{index - servos + 1}"
        table = CONTROLLER_ALARMS if index < servos else SERVO_ALARMS
        for alarm in group:
            if alarm == -2 and index < servos:
                description, solution = "Collision detected", "Check the arm for contact"
            else:
                description, solution = table.get(alarm, ("Unknown alarm", ""))
            alarms.append(
                {"id": alarm, "source": source, "description": description, "solution": solution}
            )
    return alarms
