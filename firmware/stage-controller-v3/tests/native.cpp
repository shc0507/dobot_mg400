// Runs the real control loop, encoder reader, serial parser and AccelStepper source.
// Only clocks/GPIO/I2C/UART are replaced. No hardware port is opened.
#include "Arduino.h"
#include "Wire.h"
#include "../MotorPins.h"
#include "../MotorControl.h"
#include <cassert>
#include <cstring>
#include <iostream>
#include <vector>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
#ifdef NDEBUG
#error "Native checks require assertions enabled. Compile with -UNDEBUG."
#endif
static_assert(sizeof(long)==4 && sizeof(unsigned long)==4,
              "Use a host ABI with 32-bit long (Windows or a 32-bit compiler target), matching AVR.");

namespace {
uint64_t clockUs = 0;
uint8_t outputs[20] = {};
uint8_t pinModes[20] = {};
uint16_t analogInputs[3] = {1023,1023,1023};
bool switchPlant[3] = {};
double physicalUpper[3] = {}, physicalLower[3] = {};
double physical[3] = {};
uint16_t physicalSpr[3] = {200,200,200};
long pulseCount[3] = {}, reversePulses[3] = {};
bool online[3] = {true,true,true}, magnet[3] = {true,true,true}, inverted[3] = {};
bool jammed[3] = {};
// A one-sided obstruction: the rotor bounces against it instead of freezing.
int wallSide[3] = {};
double wallAt[3] = {}, field[3] = {};
// A4988 translator microstep within one electrical cycle, 0 = Home.
int translator[3] = {};
bool homeOnWake[3] = {};
// 0: the rotor reaches the Home equilibrium 20 ms after wake. N: friction holds it
// until pulse N moves the field, as measured on the bench Bottom axis.
int snapPulses = 0;
uint64_t snapAtUs[3] = {};
long snapAtPulse[3] = {};
double pendingSnap[3] = {}; // Field minus rotor, counts.
int dropEvery[3] = {};
bool accelerationSensitive[3] = {};
double rotorPulseRate[3] = {};
uint64_t lastPlantUs[3] = {};
long accelerationDrops[3] = {};
bool noisy[3] = {};
bool checkPhysicalBounds[3] = {};
double testLower[3] = {}, testUpper[3] = {};
bool shortRaw[3] = {}, shortStatus[3] = {};
uint8_t statusOverride[3] = {255,255,255};
uint8_t registerPointer[3] = {};
unsigned int rawRequests[3] = {}, statusRequests[3] = {};
uint8_t destination=0, payload=0, channel=0, readIndex=0, readLength=0, readBuffer[8]={};
const MotorPins::PinPair pairs[] = {MotorPins::Bottom,MotorPins::Top,MotorPins::Insert};
// Model the confirmed wiring and expected polarity independently of EncoderConfig
// so a regression cannot be hidden by the simulated hardware following it.
int axisForChannel(uint8_t c) { assert(c<3); return c==0 ? 0 : c==1 ? 2 : 1; }
int divisor() {
  int bits=outputs[MotorPins::MS1] | (outputs[MotorPins::MS2]<<1) | (outputs[MotorPins::MS3]<<2);
  return bits==7 ? 16 : bits==3 ? 8 : bits==2 ? 4 : bits==1 ? 2 : 1;
}
const uint8_t sleepPins[] = {MotorPins::BottomSleep,MotorPins::TopSleep,MotorPins::InsertSleep};
// One electrical cycle is 4 full steps. Torque pushes toward the field for half a
// cycle; beyond that the rotor slips back to the equilibrium behind the obstruction.
double electricalCycle(int axis) { return 4*4096.0/physicalSpr[axis]; }
double againstWall(int axis) {
  const double lead=wallSide[axis]*(field[axis]-wallAt[axis]);
  if(lead<=0) return field[axis];
  const double cycles=lead/electricalCycle(axis);
  return cycles-std::floor(cycles)<0.5 ? wallAt[axis]
         : field[axis]-wallSide[axis]*electricalCycle(axis)*std::ceil(cycles);
}
void applySnap(int axis) {
  if((snapAtUs[axis] && clockUs>=snapAtUs[axis]) ||
     (snapAtPulse[axis] && pulseCount[axis]>=snapAtPulse[axis])) {
    // The rotor moves to the nearest equilibrium of the present field.
    const double cycle=electricalCycle(axis);
    const double snap=pendingSnap[axis]-cycle*std::floor(pendingSnap[axis]/cycle+0.5);
    physical[axis]+=snap; field[axis]+=snap;
    snapAtUs[axis]=0; snapAtPulse[axis]=0; pendingSnap[axis]=0;
  }
}
}
FakeSerial Serial;
FakeWire Wire;
double requestedPulseRate(uint8_t axis);
unsigned long millis() { return static_cast<uint32_t>(clockUs/1000); }
unsigned long micros() { return static_cast<uint32_t>(clockUs); }
void delayMicroseconds(unsigned int us) { clockUs += us; }
void pinMode(uint8_t pin,uint8_t mode) { assert(pin<20); pinModes[pin]=mode; }
int analogRead(uint8_t pin) {
  assert(pin>=A0 && pin<=A2 && pinModes[pin]==INPUT_PULLUP);
  delayMicroseconds(100); // Model the Uno ADC's approximately 100 microsecond conversion.
  const uint8_t axis=pin-A0;
  if(switchPlant[axis]) {
    if(physical[axis]>=physicalLower[axis]) return 250;
    if(physical[axis]<=physicalUpper[axis]) return 0;
    return 1023;
  }
  return analogInputs[pin-A0];
}
void digitalWrite(uint8_t pin,uint8_t level) {
  assert(pin<20);
  for (int i=0;i<3;++i) if (homeOnWake[i] && pin==sleepPins[i] && level==HIGH && outputs[pin]==LOW) {
    // SLEEP high returns the translator to Home. The rotor then moves to the nearest
    // Home equilibrium, up to 2 full steps away in either direction.
    pendingSnap[i]=-translator[i]*electricalCycle(i)/(4*divisor());
    translator[i]=0;
    if(snapPulses) snapAtPulse[i]=pulseCount[i]+snapPulses; else snapAtUs[i]=clockUs+20000;
  }
  for (int i=0;i<3;++i) if (pin==pairs[i].step && level==HIGH && outputs[pin]==LOW) {
    ++pulseCount[i];
    int sign=outputs[pairs[i].dir]==HIGH ? 1 : -1;
    if(sign<0) ++reversePulses[i];
    translator[i]=((translator[i]+sign)%(4*divisor())+4*divisor())%(4*divisor());
    const bool frictionHeld=snapAtPulse[i]!=0; // The field moves; the rotor does not.
    if(frictionHeld) pendingSnap[i]+=sign*4096.0/(physicalSpr[i]*divisor());
    applySnap(i);
    bool accelerationDrop=false;
    if(accelerationSensitive[i]) {
      // A rotor can accelerate at 500 pulses/s^2 and capture a drive within 80 pulses/s.
      // It misses steps while a sudden drive-rate jump lies outside that capture margin.
      const double requested=requestedPulseRate(i);
      const double delta=500.0*(clockUs-lastPlantUs[i])/1000000.0;
      rotorPulseRate[i]+=std::max(-delta,std::min(delta,requested-rotorPulseRate[i]));
      lastPlantUs[i]=clockUs;
      accelerationDrop=std::fabs(requested-rotorPulseRate[i])>80;
      if(accelerationDrop) ++accelerationDrops[i];
    }
    if(frictionHeld) {
    } else if(wallSide[i]) {
      field[i] += sign * 4096.0/(physicalSpr[i]*divisor());
      physical[i] = againstWall(i);
    } else if(!jammed[i] && !accelerationDrop && !(dropEvery[i] && pulseCount[i]%dropEvery[i]==0))
      physical[i] += sign * 4096.0/(physicalSpr[i]*divisor());
    if(checkPhysicalBounds[i]) {
      assert(physical[i]>=testLower[i]-1e-6);
      assert(physical[i]<=testUpper[i]+1e-6);
    }
  }
  outputs[pin]=level;
}
int digitalRead(uint8_t pin) { return outputs[pin]; }
void FakeWire::beginTransmission(uint8_t address) { destination=address; }
void FakeWire::write(uint8_t value) { payload=value; }
uint8_t FakeWire::endTransmission(bool) {
  if(destination==0x70) {
    assert(payload && !(payload&(payload-1)));
    channel=0;
    while((1U<<channel)!=payload) ++channel;
    return 0;
  }
  assert(destination==0x36);
  registerPointer[axisForChannel(channel)]=payload;
  return online[axisForChannel(channel)] ? 0 : 2;
}
uint8_t FakeWire::requestFrom(uint8_t address,uint8_t length) {
  assert(address==0x36 && length>0 && length<=sizeof(readBuffer));
  int axis=axisForChannel(channel);
  if(!online[axis]) return 0;
  applySnap(axis);
  long count=static_cast<long>(std::llround(physical[axis] * (inverted[axis] ? -1 : 1)));
  if(noisy[axis]) count += (millis()/12)%2 ? 2 : -2;
  uint16_t raw=static_cast<uint16_t>((count%4096+4096)%4096);
  const uint8_t first=registerPointer[axis];
  if(first==0x0B && length==1) ++statusRequests[axis];
  if(first==0x0C && length==2) ++rawRequests[axis];
  readLength=length;
  if((first==0x0B && shortStatus[axis]) || (first==0x0C && shortRaw[axis]))
    --readLength;
  // Real register-pointer behavior instead of manufacturing STATUS+angle bytes
  // for every request. Starting a RAW word at 0x0C loops that two-byte word.
  for(uint8_t i=0;i<readLength;++i) {
    const uint8_t reg=first==0x0C ? uint8_t(0x0C+(i%2)) : uint8_t(first+i);
    readBuffer[i]=reg==0x0B ? (statusOverride[axis]!=255 ? statusOverride[axis] : (magnet[axis] ? 0x20 : 0))
                    : reg==0x0C ? raw>>8 : reg==0x0D ? raw&255 : 0;
  }
  registerPointer[axis]=first==0x0C ? uint8_t(0x0C+(readLength%2)) : uint8_t(first+readLength);
  readIndex=0;
  return readLength;
}
int FakeWire::read() { assert(readIndex<readLength); return readBuffer[readIndex++]; }
// Compile the actual controller here so tests can inspect commanded rates and
// trajectory integration without adding firmware debug APIs or changing its code.
#include "../MotorControl.cpp"
#include "../stage-controller-v3.ino"
double requestedPulseRate(uint8_t axis) {
  return std::fabs(MotorControl::motors[axis].stepper->speed());
}
using MotorControl::Motor;
using MotorControl::Direction;
using MotorControl::Fault;
using MotorControl::Calibration;
constexpr Motor Bottom=Motor::Bottom_Motor;
MotorControl::Status state(Motor motor=Bottom) {
  MotorControl::Status result; assert(MotorControl::getStatus(motor,result)); return result;
}
void advance(unsigned long ms) {
  const uint64_t deadline=clockUs+uint64_t(ms)*1000;
  while(clockUs<deadline) { delayMicroseconds(100); MotorControl::update(); }
}
void reset() {
  clockUs=0;
  std::fill(outputs,outputs+20,0);
  for(int i=0;i<3;++i) {
    physical[i]=4080; physicalSpr[i]=200; pulseCount[i]=reversePulses[i]=0;
    accelerationSensitive[i]=false; rotorPulseRate[i]=0; lastPlantUs[i]=0; accelerationDrops[i]=0;
    analogInputs[i]=1023;
    switchPlant[i]=false;
    online[i]=magnet[i]=true; inverted[i]=(i!=0); jammed[i]=false; dropEvery[i]=0;
    wallSide[i]=0; translator[i]=0; homeOnWake[i]=false; snapAtUs[i]=snapAtPulse[i]=0; pendingSnap[i]=0;
    noisy[i]=false; checkPhysicalBounds[i]=false;
    shortRaw[i]=shortStatus[i]=false; registerPointer[i]=0;
    rawRequests[i]=statusRequests[i]=0;
    statusOverride[i]=255;
  }
  snapPulses=0;
  Serial.input.clear(); Serial.output.clear();
  Serial.roomProvider=nullptr; Serial.byteObserver=nullptr;
  commandLength=0; discardLine=false;
  MotorControl::begin();
  resetTelemetry();
}
void finish(unsigned long limit=30000,Motor motor=Bottom) {
  for(unsigned long i=0;i<limit && state(motor).rotating;i+=10) advance(10);
  if(state(motor).rotating || state(motor).fault!=Fault::None)
    std::cerr << "finite failure pos="<<state(motor).position<<" fault="<<int(state(motor).fault)<<"\n";
  assert(!state(motor).rotating && state(motor).fault==Fault::None);
}
void finite(float amount, Direction dir=Direction::Clockwise) {
  assert(MotorControl::setDirection(dir));
  float origin=state().position;
  assert(MotorControl::rotateRevolutions(amount));
  finish();
  float expected=origin+(dir==Direction::Clockwise ? -amount:amount);
  assert(std::fabs(state().position-expected)<=state().tolerance+1.0/4096);
}
void testFractionalAndWrapping() {
  reset();
  finite(0.1f); finite(0.5f); finite(2.3f); finite(3.0f,Direction::Counterclockwise);
  assert(pulseCount[0]>1000);
}
void testCorrectsLostPulsesAndOvershoot() {
  reset(); dropEvery[0]=5;
  finite(1.0f);
  assert(pulseCount[0]>1920); // Open-loop 1600 pulses would miss the target by 20%.
  reset();
  assert(MotorControl::rotateRevolutions(0.1f));
  physical[0]+=0.2*4096;
  finish();
  assert(reversePulses[0]>0);
  assert(std::fabs(state().position+0.1f)<=state().tolerance+1.0/4096);
}
void testFixedEighthAndConcurrentAxes() {
  reset();
  assert(MotorControl::microstep()==8 && divisor()==8);
  assert(outputs[MotorPins::MS1]==HIGH && outputs[MotorPins::MS2]==HIGH &&
         outputs[MotorPins::MS3]==LOW);
  assert(MotorControl::setMicrostep(8));
  for(uint8_t mode: {0,1,2,4,16,255}) {
    assert(!MotorControl::setMicrostep(mode));
    assert(MotorControl::microstep()==8 && divisor()==8);
    assert(state().position==0 && pulseCount[0]==0);
  }
  finite(.5f);
  assert(std::fabs(state().maxSpeed-.5f)<0.00001f);
  assert(state().tolerance==4.0f/4096);
  assert(pulseCount[0]>784 && pulseCount[0]<816);
  reset(); assert(MotorControl::rotateSteps(1600)); finish();
  assert(std::fabs(state().position+1)<=state().tolerance);
  reset();
  for(int axis=0;axis<3;++axis) {
    assert(MotorControl::selectMotor(static_cast<Motor>(axis)));
    assert(MotorControl::rotateRevolutions(0.5f));
  }
  assert(!MotorControl::setMicrostep(8));
  assert(!MotorControl::setMicrostep(16)); advance(6000);
  for(int axis=0;axis<3;++axis) {
    auto s=state(static_cast<Motor>(axis));
    assert(!s.rotating && s.fault==Fault::None && std::fabs(s.position+.5f)<=s.tolerance);
  }
}
void testHoldFeedbackAndWatchdog() {
  reset(); dropEvery[0]=5;
  assert(MotorControl::setSpeed(0.2f)); assert(MotorControl::startHold());
  for(int i=0;i<30;++i) { advance(200); assert(MotorControl::keepHoldAlive()); }
  // 0.8s ramp to0.2rps followed by5.2s cruising:0.5*0.2*0.8+0.2*5.2=1.12 turns.
  assert(std::fabs(state().position+1.12f)<0.04f);
  advance(1100); assert(!state().rotating); assert(!MotorControl::keepHoldAlive());
  long stopped=pulseCount[0]; advance(500); assert(pulseCount[0]==stopped);
  reset(); assert(MotorControl::startHold()); advance(300); MotorControl::stopAll();
  stopped=pulseCount[0]; advance(100); assert(pulseCount[0]==stopped);
}
void testFaultsAndRecovery() {
  reset(); jammed[0]=true; assert(MotorControl::rotateRevolutions(1)); advance(3000);
  assert(!state().rotating && state().fault==Fault::Stall);
  assert(!MotorControl::rotateRevolutions(.1f)); jammed[0]=false;
  assert(MotorControl::clearFault()); assert(!state().rotating); finite(.1f);
  reset(); inverted[0]=true; // Rebaseline the changed physical sign while idle.
  MotorControl::begin(); assert(MotorControl::rotateRevolutions(1)); advance(2000);
  assert(!state().rotating && state().fault==Fault::Direction);
  reset(); assert(MotorControl::startHold()); advance(200); online[0]=false; advance(20);
  assert(!state().rotating && state().fault==Fault::Encoder);
  assert(!MotorControl::clearFault()); online[0]=true; advance(20);
  assert(state().encoder==Encoders::Health::Untrusted);
  assert(MotorControl::clearFault()); assert(state().position==0); assert(!state().rotating);
  finite(.1f);
  reset(); assert(MotorControl::rotateRevolutions(1)); advance(100); magnet[0]=false; advance(20);
  assert(!state().rotating && state().fault==Fault::Encoder);
  reset(); assert(MotorControl::rotateRevolutions(1)); advance(100);
  delayMicroseconds(61000); MotorControl::update();
  assert(!state().rotating && state().fault==Fault::Encoder);
  reset(); jammed[0]=noisy[0]=true;
  assert(MotorControl::rotateRevolutions(.1f)); advance(3000);
  assert(!state().rotating && state().fault==Fault::Stall);
  reset(); jammed[0]=true; assert(MotorControl::setSpeed(.5f));
  assert(MotorControl::startHold());
  advance(800); assert(MotorControl::keepHoldAlive()); advance(650);
  assert(!state().rotating && state().fault==Fault::Tracking);
  reset(); online[0]=false; MotorControl::begin();
  assert(!MotorControl::rotateRevolutions(.1f)); online[0]=true; advance(30);
  assert(!MotorControl::rotateRevolutions(.1f)); assert(MotorControl::clearFault()); finite(.1f);
}
void testSlowMovesAndSleep() {
  reset(); assert(MotorControl::setSpeed(.001f));
  assert(MotorControl::rotateRevolutions(.01f)); finish(30000);
  assert(pulseCount[0]>=1 && state().fault==Fault::None);
  assert(outputs[MotorPins::BottomSleep]==HIGH);
  advance(10100); assert(outputs[MotorPins::BottomSleep]==LOW);
  long old=pulseCount[0]; assert(MotorControl::rotateRevolutions(.01f));
  delayMicroseconds(1000); MotorControl::update(); assert(pulseCount[0]==old);
  reset(); assert(MotorControl::setSpeed(.001f));
  assert(MotorControl::rotateRevolutions(.1f)); finish(120000);
  assert(std::fabs(state().position+.1f)<=state().tolerance+1.0/4096);
}
void testProtocolAndBounds() {
  reset(); printStatus();
  assert(Serial.output.find("motion=closed_loop_v1\r\nmicrostep=8")!=std::string::npos);
  assert(Serial.output.find("encoder=ok fault=none")!=std::string::npos);
  Serial.input="speed 0.05\nrev 0.1\n";
  while(Serial.available()) readSerialCommands(); finish();
  assert(Serial.output.find("OK: bottom rev")!=std::string::npos);
  assert(!MotorControl::setSpeed(NAN)); assert(!MotorControl::setSpeed(INFINITY));
  assert(!MotorControl::rotateRevolutions(0)); assert(!MotorControl::rotateRevolutions(10001));
  assert(MotorControl::setMicrostep(8)); assert(!MotorControl::setSpeed(.501f));
  assert(MotorControl::setSpeed(.001f)); assert(!MotorControl::rotateRevolutions(100));
  assert(!MotorControl::setStepsPerRevolution(0));
  assert(MotorControl::setStepsPerRevolution(65535));
  assert(state().maxSpeed>=.001f);
  assert(!MotorControl::setMicrostep(1));
  assert(!MotorControl::setMicrostep(16));
  assert(MotorControl::setStepsPerRevolution(301)); assert(MotorControl::setSpeed(.332226f));
  assert(state().speed<=state().maxSpeed);
  Serial.output.clear(); Serial.input="microstep 16\nmicrostep 8\nmicrostep\n";
  while(Serial.available()) readSerialCommands();
  assert(Serial.output.find("ERROR: All stages use fixed eighth stepping")!=std::string::npos);
  assert(Serial.output.find("OK: microstep 8")!=std::string::npos);
  assert(Serial.output.find("microstep=8")!=std::string::npos);
  assert(MotorControl::microstep()==8 && divisor()==8);
}
void testTimerRollover() {
  reset(); clockUs=(uint64_t(UINT32_MAX)-500)*1000; MotorControl::begin();
  assert(MotorControl::startHold()); advance(1200);
  assert(!state().rotating && state().fault==Fault::None);
  reset(); clockUs=uint64_t(UINT32_MAX)-500; MotorControl::begin();
  finite(.1f);
}
void testRegisterReadsAndLiveDiagnostics() {
  reset();
  const uint8_t channels[]={0,2,1};
  const uint16_t initialRaw[]={4080,16,16};
  for(int axis=0;axis<3;++axis) {
    const auto before=Encoders::get(axis);
    assert(before.channel==channels[axis] && before.raw==initialRaw[axis] && before.magnetStatus==0x20);
    assert(rawRequests[axis]>0 && statusRequests[axis]>0);
    assert(MotorControl::selectMotor(static_cast<Motor>(axis)));
    assert(MotorControl::rotateRevolutions(.1f)); advance(2500);
    const auto after=Encoders::get(axis);
    assert(!state(static_cast<Motor>(axis)).rotating);
    assert(after.raw!=before.raw && after.channel==channels[axis]);
    const long sensorCount=long(std::llround(physical[axis]*(inverted[axis] ? -1 : 1)));
    const uint16_t expected=uint16_t((sensorCount%4096+4096)%4096);
    assert(after.raw==expected && std::labs(after.counts-410)<=14);
    for(int later=axis+1;later<3;++later) assert(Encoders::get(later).raw==initialRaw[later]);
  }
  reset(); jammed[0]=true; assert(MotorControl::rotateRevolutions(1)); advance(2500);
  assert(state().fault==Fault::Stall);
  const auto faulted=Encoders::get(0);
  physical[0]+=123; advance(20);
  assert(Encoders::get(0).raw!=faulted.raw && Encoders::get(0).counts==123);
  assert(state().fault==Fault::Stall && !state().rotating);
  const long pulsesBeforeDiagnostic=pulseCount[0];
  const Motor selectionBeforeDiagnostic=MotorControl::selectedMotor();
  Serial.output.clear(); Serial.input="enc\n";
  while(Serial.available()) readSerialCommands();
  assert(Serial.output.find("ENC mux_address=112\r\n")!=std::string::npos);
  assert(Serial.output.find("ENC bottom channel=0 raw=107 angle_deg=")!=std::string::npos);
  assert(Serial.output.find("ENC top channel=2 raw=16 angle_deg=")!=std::string::npos);
  assert(Serial.output.find("ENC insert channel=1 raw=16 angle_deg=")!=std::string::npos);
  assert(Serial.output.find("magnet=ok health=ok counts=-123 age_ms=")!=std::string::npos);
  assert(state().fault==Fault::Stall && !state().rotating);
  assert(Encoders::get(0).counts==123 && pulseCount[0]==pulsesBeforeDiagnostic);
  assert(MotorControl::selectedMotor()==selectionBeforeDiagnostic);
  for(bool rawFailure: {false,true}) {
    reset(); assert(MotorControl::rotateRevolutions(1)); advance(200);
    if(rawFailure) shortRaw[0]=true; else shortStatus[0]=true;
    advance(20);
    assert(state().fault==Fault::Encoder && !state().rotating);
    assert(Encoders::get(0).health==Encoders::Health::Offline);
    assert(Encoders::get(0).magnetStatus==(rawFailure ? 0x20 : 255));
    assert(!MotorControl::clearFault());
    shortRaw[0]=shortStatus[0]=false; advance(20);
    assert(Encoders::get(0).health==Encoders::Health::Untrusted);
    assert(MotorControl::clearFault());
  }
  reset(); magnet[0]=false; advance(20);
  assert(Encoders::get(0).health==Encoders::Health::Magnet);
  assert(Encoders::get(0).magnetStatus==0);
  online[0]=false; advance(20); assert(Encoders::get(0).magnetStatus==255);
  for(uint8_t bits: {uint8_t(0x30),uint8_t(0x28)}) {
    reset(); statusOverride[0]=bits; advance(20);
    assert(Encoders::get(0).health==Encoders::Health::Magnet);
    assert(Encoders::get(0).magnetStatus==bits);
    Serial.output.clear(); Serial.input="enc\n";
    while(Serial.available()) readSerialCommands();
    assert(Serial.output.find(bits==0x30 ? "magnet=weak health=magnet" : "magnet=strong health=magnet")
           !=std::string::npos);
  }
}

struct TelemetrySample {
  unsigned long sequence, uptime, raw, channel, magnet, age;
  long counts, remaining;
  float speed;
  std::string motor, direction, mode, encoder, fault;
};
std::vector<std::string> completeLines(const std::string &text) {
  std::vector<std::string> lines;
  size_t start=0, end;
  while((end=text.find('\n',start))!=std::string::npos) {
    std::string line=text.substr(start,end-start);
    if(!line.empty() && line.back()=='\r') line.pop_back();
    lines.push_back(line); start=end+1;
  }
  return lines;
}
TelemetrySample parseTelemetry(const std::string &line) {
  std::istringstream stream(line);
  TelemetrySample s;
  std::string marker;
  assert(bool(stream>>marker>>s.sequence>>s.uptime>>s.motor>>s.direction>>s.mode));
  assert(marker=="TEL");
  auto field=[&](const char *name) {
    std::string value; assert(bool(stream>>value));
    const std::string prefix=std::string(name)+"=";
    assert(value.find(prefix)==0); return value.substr(prefix.size());
  };
  s.counts=std::stol(field("counts")); s.remaining=std::stol(field("remaining"));
  const std::string speed=field("speed");
  assert(speed.find('.')!=std::string::npos && speed.size()-speed.find('.')-1==6);
  s.speed=std::stof(speed);
  s.encoder=field("encoder"); s.fault=field("fault");
  s.raw=std::stoul(field("raw")); s.channel=std::stoul(field("channel"));
  s.magnet=std::stoul(field("magnet")); s.age=std::stoul(field("age"));
  std::string extra; assert(!(stream>>extra));
  assert(s.raw<4096 && s.channel<8 && s.magnet<=255);
  return s;
}
std::vector<TelemetrySample> samples() {
  std::vector<TelemetrySample> result;
  for(const auto &line:completeLines(Serial.output))
    if(line.find("TEL ")==0) result.push_back(parseTelemetry(line));
  return result;
}
struct LimitSample {
  unsigned long sequence, uptime;
  std::string motor, calibration, warning;
  long lower, upper, counts;
  unsigned int bounds, mask, raw, warnings;
};
LimitSample parseLimits(const std::string &line) {
  std::istringstream stream(line);
  LimitSample s;
  std::string marker;
  assert(bool(stream>>marker>>s.sequence>>s.uptime>>s.motor) && marker=="LIM");
  auto field=[&](const char *name) {
    std::string value; assert(bool(stream>>value));
    const std::string prefix=std::string(name)+"=";
    assert(value.find(prefix)==0); return value.substr(prefix.size());
  };
  s.lower=std::stol(field("lower")); s.upper=std::stol(field("upper"));
  s.bounds=std::stoul(field("bounds")); s.mask=std::stoul(field("switch"));
  s.raw=std::stoul(field("raw"));
  s.counts=std::stol(field("counts"));
  s.calibration=field("calibration");
  s.warning=field("warning"); s.warnings=std::stoul(field("warnings"));
  assert((s.warning=="none" || s.warning=="direction_pause") && s.warnings<=65535);
  assert(s.calibration=="none" || s.calibration=="lower" || s.calibration=="upper" ||
         s.calibration=="center" || s.calibration=="done" || s.calibration=="cancelled" || s.calibration=="failed");
  std::string extra; assert(!(stream>>extra));
  assert(s.lower>=-1000000000 && s.upper<=1000000000 && s.lower<=s.upper);
  assert(s.bounds<=3 && s.mask<=3 && s.raw<=1023);
  assert(s.counts>=-1000000000 && s.counts<=1000000000);
  if(!(s.bounds&1)) assert(s.lower==-1000000000);
  if(!(s.bounds&2)) assert(s.upper==1000000000);
  return s;
}
std::vector<LimitSample> limitSamples() {
  std::vector<LimitSample> result;
  for(const auto &line:completeLines(Serial.output))
    if(line.find("LIM ")==0) result.push_back(parseLimits(line));
  return result;
}
void advanceLoop(unsigned long ms) {
  const uint64_t deadline=clockUs+uint64_t(ms)*1000;
  while(clockUs<deadline) { delayMicroseconds(100); loop(); }
}
uint64_t lastUartByte=0;
bool checkPendingLineBeforeMove=false;
int slowUartRoom() {
  if(clockUs-lastUartByte<87) { delayMicroseconds(20); return 0; }
  return 1;
}
int blockedUartRoom() { return 0; }
void observeUartByte(uint8_t value) {
  lastUartByte=clockUs;
  if(checkPendingLineBeforeMove && value=='\n') {
    assert(!state().rotating); // A pending TEL must complete before rev executes.
    checkPendingLineBeforeMove=false;
  }
}
void testAutonomousTelemetryAndCommands() {
  reset(); advanceLoop(1000);
  auto idle=samples(); assert(idle.size()>=29 && idle.size()<=31);
  unsigned int bottomLines=0, topLines=0, insertLines=0;
  unsigned long previousTime=0, previousSequence=0;
  for(const auto &s:idle) {
    assert(s.sequence==previousSequence+(previousSequence ? 2 : 1) && s.uptime-previousTime>=33);
    previousSequence=s.sequence; previousTime=s.uptime;
    assert(s.mode=="idle" && s.counts==0 && s.remaining==0 && s.speed==0);
    assert(s.encoder=="ok" && s.fault=="none" && s.magnet==32 && s.age<=12);
    assert(s.raw==(s.motor=="bottom" ? 4080U : 16U));
    if(s.motor=="bottom") { ++bottomLines; assert(s.channel==0); }
    else if(s.motor=="top") { ++topLines; assert(s.channel==2); }
    else { ++insertLines; assert(s.motor=="insert" && s.channel==1); }
  }
  assert(bottomLines>=9 && topLines>=9 && insertLines>=9);

  reset(); Serial.input="motor top\nspeed 0.2\nrev 0.5\n"; advanceLoop(4500);
  bool sawMoving=false, sawCompleted=false;
  for(const auto &s:samples()) {
    if(s.motor=="top" && s.mode=="moving" && s.counts<-100 && s.remaining<0) sawMoving=true;
    if(s.motor=="top" && s.mode=="idle" && std::labs(s.counts+2048)<=4) sawCompleted=true;
    if(s.motor!="top") assert(s.counts==0 && s.mode=="idle");
  }
  assert(sawMoving && sawCompleted && Serial.output.find("OK: top rev\r\n")!=std::string::npos);

  reset(); Serial.input="dir ccw\nrev 0.1\n"; advanceLoop(2500);
  bool sawReverse=false;
  for(const auto &s:samples()) if(s.motor=="bottom" && s.mode=="moving" && s.counts>10) {
    assert(s.direction=="ccw" && s.remaining>0 && s.speed>0); sawReverse=true;
  }
  assert(sawReverse);

  // Begin a TEL line, then receive a complete command while UART space is scarce.
  reset(); delayMicroseconds(33000); serviceTelemetry();
  assert(telemetryLine.sent>0 && telemetryLine.sent<telemetryLine.length);
  Serial.roomProvider=slowUartRoom; Serial.byteObserver=observeUartByte;
  lastUartByte=clockUs; checkPendingLineBeforeMove=true;
  Serial.input="status\nrev 0.1\n"; advanceLoop(20);
  auto lines=completeLines(Serial.output);
  assert(!checkPendingLineBeforeMove && lines.size()>=15);
  parseTelemetry(lines[0]);
  assert(lines[1]=="warnings=held_direction_v1" && lines[2]=="positioning=calibrated_absolute_v1" &&
         lines[3]=="calibration=switch_midpoint_v1" && lines[4]=="coordinates=ccw_positive_v1" &&
         lines[5]=="homing=calibration_only_v1" && lines[6]=="switches=analog_pair_v2" &&
         lines[7]=="limits=directional_v2" && lines[8]=="telemetry=positions_v1" &&
         lines[9]=="motion=closed_loop_v1" && lines[10]=="microstep=8");
  assert(lines[11].find("bottom ")!=std::string::npos && lines[12].find("top ")!=std::string::npos &&
         lines[13].find("insert ")!=std::string::npos && lines[14]=="OK: bottom rev");
  advanceLoop(2500); assert(!state().rotating && std::fabs(state().position+.1f)<=state().tolerance);
  for(const auto &line:completeLines(Serial.output)) {
    if(line.find("TEL ")==0) parseTelemetry(line);
    else assert(line.find("TEL ")==std::string::npos); // No TEL bytes inside ACK/status.
  }

  reset(); jammed[0]=true; Serial.input="rev 1\n"; advanceLoop(2500);
  bool sawFault=false;
  for(const auto &s:samples()) if(s.motor=="bottom" && s.fault=="stall") {
    assert(s.mode=="idle" && s.counts==0); sawFault=true;
  }
  assert(sawFault); physical[0]+=1024; advanceLoop(200);
  bool sawFaultedMotion=false;
  for(const auto &s:samples()) if(s.motor=="bottom" && s.counts==-1024) {
    assert(s.mode=="idle" && s.fault=="stall"); sawFaultedMotion=true;
  }
  assert(sawFaultedMotion);

  reset(); Serial.roomProvider=blockedUartRoom;
  assert(MotorControl::rotateRevolutions(.1f)); advanceLoop(500);
  assert(state().position<0 && telemetrySequence==0 && telemetryLine.length==0);
  Serial.roomProvider=nullptr; advanceLoop(200);
  auto resumed=samples(); assert(!resumed.empty() && resumed[0].uptime>=500);

  // Sequence and scheduling rollover retain complete integer telemetry.
  reset(); clockUs=(uint64_t(UINT32_MAX)-50)*1000; MotorControl::begin(); resetTelemetry();
  telemetrySequence=UINT32_MAX-1; advanceLoop(150);
  auto wrapped=samples(); assert(wrapped.size()>=4);
  assert(wrapped[0].sequence==UINT32_MAX && wrapped[1].sequence==1);
  assert(wrapped[1].uptime<100);
}
void testConfirmedWiringAndAxisDirections() {
  // Moving one physical shaft must change only its matching logical encoder.
  for(int axis=0;axis<3;++axis) {
    reset(); physical[axis]+=256; advance(24);
    for(int observed=0;observed<3;++observed)
      assert(Encoders::get(observed).counts==(axis==observed ? 256 : 0));
  }
  const char *names[]={"bottom","top","insert"};
  for(int axis=0;axis<3;++axis) {
    reset();
    Serial.input=std::string("motor ")+names[axis]+"\ndir cw\nspeed 0.2\nrev 0.5\n";
    advanceLoop(4500);
    auto motor=state(static_cast<Motor>(axis));
    assert(!motor.rotating && motor.fault==Fault::None);
    assert(std::fabs(motor.position+.5f)<=motor.tolerance);
    bool cwNegativeSpeed=false;
    for(const auto &s:samples()) {
      assert(s.fault=="none");
      if(s.motor==names[axis] && s.mode=="moving" && s.speed<0) cwNegativeSpeed=true;
      if(s.motor!=names[axis]) assert(s.counts==0 && s.mode=="idle");
    }
    assert(cwNegativeSpeed);
    Serial.output.clear();
    Serial.input="dir ccw\nrev 1\n"; advanceLoop(7500);
    motor=state(static_cast<Motor>(axis));
    assert(!motor.rotating && motor.fault==Fault::None);
    assert(std::fabs(motor.position-.5f)<=2*motor.tolerance);
    bool ccwPositiveSpeed=false;
    for(const auto &s:samples()) {
      assert(s.fault=="none");
      if(s.motor==names[axis] && s.mode=="moving" && s.speed>0) ccwPositiveSpeed=true;
      if(s.motor!=names[axis]) assert(s.counts==0 && s.mode=="idle");
    }
    assert(ccwPositiveSpeed);
    for(int other=0;other<3;++other) if(other!=axis) assert(pulseCount[other]==0);
  }
}
void testInsertOutQuarterTurn() {
  // Reproduce the user's command with a sensor that decreases for DIR HIGH.
  // The independent hardware model above must not copy EncoderConfig's flags.
  reset();
  const double origin=physical[2];
  Serial.input="motor insert\ndir cw\nspeed 0.25\nrev 0.25\n";
  advanceLoop(2500);
  const auto insert=state(Motor::Insert_Motor);
  assert(!insert.rotating && insert.fault==Fault::None);
  assert(std::fabs(insert.position+.25f)<=insert.tolerance);
  assert(std::fabs((physical[2]-origin)/4096-.25)<=insert.tolerance);
  assert(std::labs(long(Encoders::get(2).raw)-3088)<=4);
  assert(pulseCount[0]==0 && pulseCount[1]==0);
  bool moving=false, completed=false;
  for(const auto &s:samples()) if(s.motor=="insert") {
    assert(s.fault=="none" && s.channel==1 && s.counts<=0);
    if(s.mode=="moving" && s.counts<-36 && s.speed<0) moving=true;
    if(s.mode=="idle" && std::labs(s.counts+1024)<=4) completed=true;
  }
  assert(moving && completed);
}
void manualPosition(uint8_t axis,long counts) {
  // This hardware model and Encoders internals remain CW-positive; public Status is the opposite.
  assert(Encoders::refresh(axis));
  physical[axis] += counts-Encoders::get(axis).counts;
  assert(Encoders::refresh(axis));
  assert(Encoders::get(axis).counts==counts);
}
void walkEncoderTo(uint8_t axis,long counts) {
  // Reach count-range edges through unambiguous encoder reads, without test-only firmware hooks.
  assert(Encoders::refresh(axis));
  while(Encoders::get(axis).counts!=counts) {
    const long change=std::max(-1000L,std::min(1000L,counts-Encoders::get(axis).counts));
    physical[axis]+=change;
    assert(Encoders::refresh(axis));
  }
}
void captureTestBounds(long minimum,long maximum) {
  assert(MotorControl::selectMotor(Bottom));
  manualPosition(0,maximum); assert(MotorControl::setLimit());
  manualPosition(0,minimum); assert(MotorControl::setLimit());
  manualPosition(0,0);
  assert(state().upperCounts==-minimum && state().lowerCounts==-maximum && state().bounds==3);
}
void testLimitCaptureResetAndIsolation() {
  reset();
  assert(state().upperCounts==1000000000 && state().lowerCounts==-1000000000 && state().bounds==0);
  assert(!MotorControl::setLimit());
  physical[0]+=500; // Capturing samples immediately, without waiting for update().
  assert(MotorControl::setLimit());
  assert(state().positionCounts==-500 && state().lowerCounts==-500 && state().bounds==1);
  physical[0]-=1000;
  assert(MotorControl::setLimit());
  assert(state().upperCounts==500 && state().bounds==3);
  assert(MotorControl::clearLowerLimit() && MotorControl::clearLowerLimit());
  assert(state().upperCounts==500 && state().lowerCounts==-1000000000 && state().bounds==2);
  assert(MotorControl::clearUpperLimit() && MotorControl::clearUpperLimit());
  assert(state().bounds==0 && state().upperCounts==1000000000);
  captureTestBounds(-500,500);
  assert(MotorControl::selectMotor(Motor::Top_Motor));
  manualPosition(1,300); assert(MotorControl::setLimit());
  assert(MotorControl::selectMotor(Bottom));
  manualPosition(0,100);
  const double before=physical[0]; const long pulses=pulseCount[0];
  assert(MotorControl::resetPosition());
  assert(state().positionCounts==0 && state().bounds==0);
  assert(state().upperCounts==1000000000 && state().lowerCounts==-1000000000);
  assert(physical[0]==before && pulseCount[0]==pulses);
  assert(state(Motor::Top_Motor).positionCounts==-300 && state(Motor::Top_Motor).lowerCounts==-300);
  physical[0]+=50; assert(Encoders::refresh(0)); assert(state().positionCounts==-50);
  assert(MotorControl::startHold());
  assert(!MotorControl::resetPosition() && !MotorControl::setLimit());
  assert(!MotorControl::clearLowerLimit() && !MotorControl::clearUpperLimit());
  MotorControl::stopRotation();
  reset(); captureTestBounds(-500,500);
  jammed[0]=true; assert(MotorControl::rotateRevolutions(.1f)); advance(2500);
  assert(state().fault==Fault::Stall);
  manualPosition(0,100);
  assert(MotorControl::resetPosition());
  assert(state().positionCounts==0 && state().bounds==0 && state().fault==Fault::Stall);
}
void testLimitEnforcementAndRecoveryDirection() {
  reset(); captureTestBounds(-500,500);
  assert(!MotorControl::rotateRevolutions(.25f) && !MotorControl::rotateSteps(1600));
  assert(pulseCount[0]==0 && !state().rotating);
  manualPosition(0,500);
  assert(!MotorControl::startHold() && !MotorControl::startRotation());
  assert(!MotorControl::rotateSteps(10));
  manualPosition(0,700);
  assert(!MotorControl::rotateRevolutions(.01f));
  assert(MotorControl::setDirection(Direction::Counterclockwise));
  assert(!MotorControl::rotateRevolutions(.5f)); // Would cross the opposite bound.
  assert(MotorControl::rotateRevolutions(.01f)); finish();
  assert(state().positionCounts<-500 && state().positionCounts>-700);
  assert(MotorControl::startRotation()); advance(100); MotorControl::stopRotation();
  manualPosition(0,-700);
  assert(!MotorControl::startHold() && !MotorControl::rotateRevolutions(.01f));
  assert(MotorControl::setDirection(Direction::Clockwise));
  assert(!MotorControl::rotateRevolutions(.5f));
  assert(MotorControl::rotateRevolutions(.01f)); finish();
  assert(state().positionCounts>500 && state().positionCounts<700);
  for(bool jitter: {false,true}) for(Direction dir: {Direction::Clockwise,Direction::Counterclockwise}) {
    reset(); captureTestBounds(-100,100);
    assert(MotorControl::setAcceleration(50) && MotorControl::setSpeed(.5f));
    assert(MotorControl::setDirection(dir));
    noisy[0]=jitter;
    checkPhysicalBounds[0]=true; testLower[0]=4080-100; testUpper[0]=4080+100;
    assert(MotorControl::startHold());
    for(int i=0;i<200 && state().rotating;++i) {
      advance(10);
      if(i%10==0 && state().rotating) assert(MotorControl::keepHoldAlive());
    }
    assert(!state().rotating && state().fault==Fault::None);
    assert(std::fabs(std::fabs(physical[0]-4080)-100)<10);
    assert(!MotorControl::keepHoldAlive());
  }
  reset(); captureTestBounds(-100,100);
  checkPhysicalBounds[0]=true; testLower[0]=3980; testUpper[0]=4180;
  dropEvery[0]=5;
  assert(MotorControl::rotateRevolutions(100.0f/4096)); finish();
  assert(std::labs(state().positionCounts+100)<=4);
  reset(); captureTestBounds(-98,98);
  physicalSpr[0]=100;
  assert(MotorControl::setStepsPerRevolution(100));
  assert(state().tolerance==5.0f/4096);
  checkPhysicalBounds[0]=true; testLower[0]=4080-98; testUpper[0]=4080+98;
  assert(MotorControl::rotateRevolutions(98.0f/4096)); advance(3000);
  assert(!state().rotating && state().fault==Fault::Tracking);
  assert(std::labs(state().positionCounts+98)>5);
  reset(); captureTestBounds(-500,500); manualPosition(0,700);
  assert(MotorControl::setDirection(Direction::Counterclockwise));
  assert(MotorControl::rotateRevolutions(.01f));
  physical[0]-=100; advance(50); // Overshot inward target while still beyond the CW/lower limit.
  assert(!state().rotating && state().fault==Fault::Tracking);
  assert(pulseCount[0]==0); // Outward correction remains forbidden.
}
void testLimitTrackingLossAndSerialContract() {
  reset(); captureTestBounds(-500,500);
  assert(MotorControl::rotateRevolutions(.1f)); advance(100);
  online[0]=false; advance(20); assert(state().fault==Fault::Encoder);
  online[0]=true; advance(20);
  const long counts=state().positionCounts;
  assert(!MotorControl::clearFault() && !MotorControl::resetPosition() && !MotorControl::setLimit());
  assert(state().positionCounts==counts && state().bounds==3);
  assert(MotorControl::clearLowerLimit() && !MotorControl::clearFault());
  assert(MotorControl::clearUpperLimit() && MotorControl::clearFault());
  assert(state().positionCounts==0 && state().bounds==0 && state().fault==Fault::None);
  reset(); captureTestBounds(-500,500); online[0]=false; advance(20);
  assert(MotorControl::clearLowerLimit() && MotorControl::clearUpperLimit());
  assert(!MotorControl::resetPosition() && !MotorControl::setLimit());
  reset(); manualPosition(0,100);
  Serial.input="setlimit\nstatus\nreset\nclearupper\nclearlower\nstatus\n";
  advanceLoop(100);
  for(const char *command: {"setlimit","reset","clearupper","clearlower"})
    assert(Serial.output.find(std::string("OK: bottom ")+command)!=std::string::npos);
  assert(Serial.output.find("limits=directional_v2\r\ntelemetry=positions_v1")!=std::string::npos);
  assert(Serial.output.find(" lower=-100 upper=1000000000 bounds=1")!=std::string::npos);
  assert(Serial.output.find(" lower=-1000000000 upper=1000000000 bounds=0")!=std::string::npos);
  assert(state().positionCounts==0 && pulseCount[0]==0);
}
void testSixAnalogSwitchesAndThresholds() {
  for(uint16_t raw: {0,99,100,699,700,1023}) {
    reset(); analogInputs[0]=raw; MotorControl::begin();
    const uint8_t expected=raw<100 ? 2 : raw<700 ? 1 : 0;
    assert(pinModes[A0]==INPUT_PULLUP && pinModes[A1]==INPUT_PULLUP && pinModes[A2]==INPUT_PULLUP);
    assert(state().switchMask==expected && state().switchRaw==raw);
    if(expected) {
      assert(MotorControl::setDirection(expected==2 ? Direction::Counterclockwise : Direction::Clockwise));
      assert(!MotorControl::startHold()); // Block from the first sample, before debounce.
      assert(state().bounds==0);
    }
    advance(25);
    assert(state().bounds==0 && pulseCount[0]==0 && state().positionCounts==0);
    assert(state().lowerCounts==-1000000000 && state().upperCounts==1000000000);
  }
  for(uint8_t axis=0;axis<3;++axis) for(uint8_t side: {1,2}) {
    reset();
    const uint8_t mask=side==1 ? 2 : 1; // Physical direct contact is upper; resistor contact is lower.
    const Motor motor=static_cast<Motor>(axis), other=static_cast<Motor>((axis+1)%3);
    assert(MotorControl::selectMotor(other) && MotorControl::startHold());
    assert(MotorControl::selectMotor(motor));
    const Direction outward=side==1 ? Direction::Counterclockwise : Direction::Clockwise;
    const Direction inward=side==1 ? Direction::Clockwise : Direction::Counterclockwise;
    assert(MotorControl::setDirection(outward) && MotorControl::startHold());
    advance(60);
    assert(outputs[pairs[axis].dir]==(side==1 ? LOW : HIGH));
    assert(side==1 ? physical[axis]<4080 : physical[axis]>4080);
    const long before=pulseCount[axis], otherBefore=pulseCount[(axis+1)%3];
    analogInputs[axis]=side==1 ? 0 : 250;
    delayMicroseconds(1000); MotorControl::update();
    assert(!state(motor).rotating && state(motor).switchMask==mask && pulseCount[axis]==before);
    assert(state(motor).bounds==0 && !MotorControl::keepHoldAlive());
    advance(25);
    assert(state(motor).bounds==0 && state(motor).fault==Fault::None);
    assert(state(motor).lowerCounts==-1000000000 && state(motor).upperCounts==1000000000);
    assert(pulseCount[axis]==before && state(other).rotating && pulseCount[(axis+1)%3]>otherBefore);
    assert(!MotorControl::startHold() && !MotorControl::rotateRevolutions(.1f));
    assert(side==1 ? MotorControl::clearUpperLimit() : MotorControl::clearLowerLimit());
    assert(!MotorControl::startRotation());
    assert(MotorControl::resetPosition()); advance(30);
    assert(state(motor).positionCounts==0 && state(motor).bounds==0 && state(motor).switchMask==mask);
    assert(!MotorControl::rotateSteps(160));
    assert(MotorControl::setDirection(inward) && MotorControl::startHold());
    advance(60); MotorControl::stopRotation();
    assert(outputs[pairs[axis].dir]==(side==1 ? HIGH : LOW));
    assert(state(motor).bounds==0 && pulseCount[axis]>before);
    assert(side==1 ? state(motor).positionCounts<0 : state(motor).positionCounts>0);
  }
}
void testSwitchBounceResetEscapeAndCorrection() {
  reset(); assert(MotorControl::startHold()); advance(50);
  analogInputs[0]=250; advance(2);
  assert(!state().rotating && state().bounds==0 && state().switchMask==1);
  const long stopped=pulseCount[0];
  analogInputs[0]=1023; advance(30);
  assert(!state().rotating && state().bounds==0 && pulseCount[0]==stopped);
  analogInputs[0]=250; advance(25);
  assert(state().bounds==0);
  analogInputs[0]=1023; advance(10); assert(state().switchMask==1);
  analogInputs[0]=250; advance(25); assert(state().switchMask==1);
  // Reset/clear remain explicit; subsequent bounce or confirmed contacts never recapture bounds.
  for(bool resetting: {false,true}) {
    reset(); manualPosition(0,100); analogInputs[0]=250; advance(2);
    assert(resetting ? MotorControl::resetPosition() : MotorControl::clearLowerLimit());
    analogInputs[0]=1023; advance(2); analogInputs[0]=250; advance(30);
    assert(state().bounds==0 && state().switchMask==1 && !MotorControl::startHold());
    analogInputs[0]=1023; advance(25); analogInputs[0]=250; advance(25);
    assert(state().bounds==0 && state().positionCounts==(resetting ? 0 : -100));
  }
  // A changing analog classification temporarily blocks both directions.
  reset(); analogInputs[0]=0; advance(25);
  analogInputs[0]=250; advance(2); assert(state().switchMask==3);
  assert(!MotorControl::startHold());
  assert(MotorControl::setDirection(Direction::Counterclockwise) && !MotorControl::startHold());
  advance(25); assert(state().switchMask==1 && state().bounds==0);
  for(bool alreadyEscaping: {false,true}) {
    reset(); assert(MotorControl::setDirection(Direction::Counterclockwise));
    if(alreadyEscaping) { assert(MotorControl::startHold()); advance(30); }
    analogInputs[0]=250; advance(2);
    if(!alreadyEscaping) assert(MotorControl::startHold());
    advance(100); MotorControl::stopRotation(); advance(30);
    assert(state().bounds==0 && state().positionCounts>0 && state().switchMask==1);
    analogInputs[0]=1023; advance(25); analogInputs[0]=250; advance(25);
    assert(state().bounds==0 && state().positionCounts>0);
  }
  // A CW finite command can reverse after overshoot: upper input blocks those actual CCW pulses.
  reset(); analogInputs[0]=0; advance(2);
  assert(MotorControl::rotateRevolutions(.1f));
  physical[0]+=0.2*4096;
  advance(40);
  assert(!state().rotating && reversePulses[0]==0 && state().bounds==0);
  // millis rollover must not prematurely confirm a contact.
  reset(); clockUs=(uint64_t(UINT32_MAX)-10)*1000; MotorControl::begin();
  analogInputs[0]=250; advance(5); assert(state().bounds==0);
  advance(25); assert(state().bounds==0 && state().switchMask==1);
}
void testSwitchContactsPreserveCoordinatesAndFaults() {
  for(uint8_t side: {1,2}) {
    const uint8_t mask=side==1 ? 2 : 1;
    reset(); manualPosition(0,side==1 ? 100 : -100);
    analogInputs[0]=side==1 ? 0 : 250; advance(25);
    assert(state().bounds==0 && state().fault==Fault::None && state().switchMask==mask);
    assert(state().positionCounts==(side==1 ? -100 : 100));
  }
  reset(); manualPosition(0,100); assert(MotorControl::setLimit());
  manualPosition(0,200); analogInputs[0]=0; advance(25);
  assert(state().fault==Fault::None && state().bounds==1 && state().lowerCounts==-100);
  assert(state().upperCounts==1000000000);
  reset(); manualPosition(0,100); online[0]=false; analogInputs[0]=250; advance(25);
  assert(state().fault==Fault::None && state().encoder==Encoders::Health::Offline && state().bounds==0);
  assert(state().switchMask==1 && pulseCount[0]==0);
}
void testSwitchLimitStreamingAndFraming() {
  reset(); advanceLoop(1000);
  const auto limits=limitSamples();
  assert(limits.size()>=29 && limits.size()<=31);
  unsigned long previous=0;
  for(const auto &line:completeLines(Serial.output)) {
    unsigned long sequence=line.find("TEL ")==0 ? parseTelemetry(line).sequence : parseLimits(line).sequence;
    assert(sequence==previous+1); previous=sequence;
  }
  for(const auto &s:limits) assert(s.bounds==0 && s.mask==0 && s.raw==1023);
  analogInputs[1]=250; advanceLoop(250);
  bool reported=false;
  for(const auto &s:limitSamples()) if(s.motor=="top" && s.mask==1 && s.bounds==0 && s.counts==0) reported=true;
  assert(reported && pulseCount[1]==0);
  // Finish a partially emitted LIM before executing a command, just as for TEL.
  reset(); delayMicroseconds(33000); serviceTelemetry();
  while(telemetryLine.length) drainTelemetry();
  serviceTelemetry();
  assert(telemetryLine.sent>0 && telemetryLine.sent<telemetryLine.length && telemetryLine.data[0]=='L');
  Serial.roomProvider=slowUartRoom; Serial.byteObserver=observeUartByte;
  lastUartByte=clockUs; checkPendingLineBeforeMove=true;
  Serial.input="rev 0.1\n"; advanceLoop(2500);
  assert(!checkPendingLineBeforeMove && !state().rotating && state().fault==Fault::None);
  for(const auto &line:completeLines(Serial.output)) {
    if(line.find("TEL ")==0) parseTelemetry(line);
    else if(line.find("LIM ")==0) parseLimits(line);
    else assert(line=="OK: bottom rev");
  }
  printStatus();
  assert(Serial.output.find("switches=analog_pair_v2\r\nlimits=directional_v2")!=std::string::npos);
  assert(Serial.output.find(" switch=0 switch_raw=1023 calibration=none warning=none warnings=0\r\n")!=std::string::npos);
}
void testDirectionalClearSerialSides() {
  reset(); captureTestBounds(-100,100);
  Serial.input="clearlower\n"; advanceLoop(10);
  assert(state().lowerCounts==-1000000000 && state().upperCounts==100 && state().bounds==2);
  assert(MotorControl::rotateRevolutions(.1f)); finish();
  assert(state().positionCounts<-100 && outputs[MotorPins::Bottom.dir]==HIGH);
  assert(MotorControl::setDirection(Direction::Counterclockwise));
  assert(!MotorControl::rotateRevolutions(.5f)); // The upper/CCW bound was retained.
  Serial.input="clearupper\n"; advanceLoop(10);
  assert(state().upperCounts==1000000000 && state().bounds==0);
  assert(MotorControl::rotateRevolutions(.5f)); finish();
  assert(state().positionCounts>100 && outputs[MotorPins::Bottom.dir]==LOW);
  assert(Serial.output.find("OK: bottom clearlower")!=std::string::npos);
  assert(Serial.output.find("OK: bottom clearupper")!=std::string::npos);
}
void testIdleContactsPreserveBoundsAndIsolateAxes() {
  for(uint8_t axis=0;axis<3;++axis) for(bool saved: {false,true})
    for(long contact: {-500L,500L}) for(uint8_t mask: {1,2}) {
      reset(); const Motor motor=static_cast<Motor>(axis);
      assert(MotorControl::selectMotor(motor));
      if(saved) {
        manualPosition(axis,-700); assert(MotorControl::setLimit());
        manualPosition(axis,700); assert(MotorControl::setLimit());
      }
      const auto before=state(motor);
      manualPosition(axis,contact); // Physical hand movement must keep accumulating normally.
      analogInputs[axis]=mask==1 ? 250 : 0; advance(25);
      assert(state(motor).positionCounts==-contact && state(motor).switchMask==mask);
      physical[axis]+=17; advance(30);
      assert(state(motor).positionCounts==-contact-17 && state(motor).fault==Fault::None);
      assert(state(motor).lowerCounts==before.lowerCounts && state(motor).upperCounts==before.upperCounts);
      assert(state(motor).bounds==before.bounds && pulseCount[axis]==0);
      analogInputs[axis]=1023; advance(25); analogInputs[axis]=mask==1 ? 250 : 0; advance(25);
      assert(state(motor).positionCounts==-contact-17 && state(motor).bounds==before.bounds);
      for(uint8_t other=0;other<3;++other) if(other!=axis) {
        const auto isolated=state(static_cast<Motor>(other));
        assert(isolated.positionCounts==0 && isolated.bounds==0 && isolated.switchMask==0 && pulseCount[other]==0);
      }
    }
}
void testExplicitZeroAndOrdinaryContactFaultPreservation() {
  reset(); manualPosition(0,100);
  const auto original=Encoders::get(0); const auto reads=rawRequests[0];
  assert(!Encoders::zeroFromSample(0,101) && !Encoders::zeroFromSample(255,100));
  assert(Encoders::get(0).counts==100 && rawRequests[0]==reads);
  assert(Encoders::zeroFromSample(0,100));
  assert(rawRequests[0]==reads && Encoders::get(0).raw==original.raw);
  assert(Encoders::get(0).sampledAt==original.sampledAt && Encoders::get(0).velocity==0);
  physical[0]+=17; assert(Encoders::refresh(0)); assert(state().positionCounts==-17);
  reset(); manualPosition(0,-300); assert(MotorControl::setLimit());
  manualPosition(0,500); analogInputs[0]=250; advance(25);
  assert(state().upperCounts==300 && state().positionCounts==-500 && state().bounds==2);
  physical[0]+=10; advance(40);
  assert(state().positionCounts==-510 && state().upperCounts==300);
  analogInputs[0]=1023; advance(5); analogInputs[0]=250; advance(30);
  assert(state().positionCounts==-510 && state().upperCounts==300);
  analogInputs[0]=1023; advance(25); physical[0]+=20;
  analogInputs[0]=250; advance(25);
  assert(state().positionCounts==-530 && state().upperCounts==300 && state().bounds==2);
  assert(MotorControl::resetPosition()); advance(25);
  assert(state().positionCounts==0 && state().bounds==0 && state().switchMask==1);
  assert(!MotorControl::startRotation()); // Explicit reset still cannot bypass a held interlock.
  reset(); captureTestBounds(-100,500);
  jammed[0]=true; assert(MotorControl::rotateRevolutions(.1f)); advance(2500);
  assert(state().fault==Fault::Stall);
  manualPosition(0,200); analogInputs[0]=250; advance(25);
  assert(state().fault==Fault::Stall && state().positionCounts==-200 && state().upperCounts==100);
  // An idle contact does not invent a fault or a new coordinate from unusable feedback.
  reset(); captureTestBounds(-300,800); manualPosition(0,500);
  online[0]=false; analogInputs[0]=250; advance(25);
  assert(state().fault==Fault::None && state().encoder==Encoders::Health::Offline && state().positionCounts==-500);
  assert(state().lowerCounts==-800 && state().upperCounts==300 && state().bounds==3);
  // Contacts beyond saved limits cannot cause a capture conflict or rebase overflow.
  reset(); manualPosition(0,-100); assert(MotorControl::setLimit()); manualPosition(0,-200);
  analogInputs[0]=250; advance(25);
  assert(state().fault==Fault::None && state().positionCounts==200);
  assert(state().upperCounts==100 && state().lowerCounts==-1000000000 && state().bounds==2);
  reset(); walkEncoderTo(0,-1000000000); assert(MotorControl::setLimit());
  walkEncoderTo(0,1); analogInputs[0]=250; advance(25);
  assert(state().fault==Fault::None && state().positionCounts==-1 && state().bounds==2);
  assert(state().upperCounts==1000000000 && state().lowerCounts==-1000000000);
}
void testContactStreamingAndAllMotionRelease() {
  reset(); manualPosition(0,-300); assert(MotorControl::setLimit());
  manualPosition(0,500); advanceLoop(150);
  analogInputs[0]=250; advanceLoop(250);
  bool before=false, after=false;
  for(const auto &s:limitSamples()) if(s.motor=="bottom") {
    assert(s.lower==-1000000000 && s.upper==300 && s.bounds==2 && s.counts==-500);
    if(s.mask==0) before=true;
    if(s.mask==1) after=true;
  }
  assert(before && after);
  assert(MotorControl::resetPosition()); advanceLoop(150);
  assert(state().bounds==0 && state().upperCounts==1000000000 && state().lowerCounts==-1000000000);
  // Reaching an ordinary saved lower bound is not a homing operation.
  reset(); manualPosition(0,100); assert(MotorControl::setLimit()); manualPosition(0,0);
  assert(MotorControl::startHold());
  for(int i=0;i<100 && state().rotating;++i) {
    advance(10); if(i%10==0 && state().rotating) assert(MotorControl::keepHoldAlive());
  }
  assert(!state().rotating && state().positionCounts<-90 && state().lowerCounts==-100);
  assert(state().bounds==1 && state().switchMask==0);
  for(int mode=0;mode<3;++mode) for(Direction dir: {Direction::Clockwise,Direction::Counterclockwise}) {
    reset(); assert(MotorControl::setDirection(dir));
    assert(mode==0 ? MotorControl::rotateRevolutions(1) : mode==1 ? MotorControl::startHold() : MotorControl::startRotation());
    advance(150); assert(Encoders::refresh(0));
    const long stopped=pulseCount[0], measured=state().positionCounts;
    analogInputs[0]=dir==Direction::Clockwise ? 250 : 0;
    delayMicroseconds(1000); MotorControl::update(); advance(25);
    assert(!state().rotating && state().bounds==0 && state().fault==Fault::None);
    assert(pulseCount[0]==stopped && std::labs(state().positionCounts-measured)<=3);
    assert(!MotorControl::startRotation());
    analogInputs[0]=1023; advance(10); assert(!MotorControl::startRotation());
    advance(20); assert(!state().rotating && pulseCount[0]==stopped && state().switchMask==0);
    assert(MotorControl::startRotation()); advance(100); MotorControl::stopRotation();
    assert(pulseCount[0]>stopped && state().bounds==0 && state().fault==Fault::None);
  }
}
void testUpperTravelPositiveFromLowerOnEveryAxis() {
  const char *names[]={"bottom","top","insert"};
  for(uint8_t axis=0;axis<3;++axis) {
    reset(); const Motor motor=static_cast<Motor>(axis);
    assert(MotorControl::selectMotor(motor));
    manualPosition(axis,-819); assert(MotorControl::setLimit());
    assert(state(motor).positionCounts==819 && state(motor).upperCounts==819);
    manualPosition(axis,205); analogInputs[axis]=250; advanceLoop(30);
    assert(state(motor).positionCounts==-205 && state(motor).upperCounts==819);
    assert(MotorControl::resetPosition()); // Only an explicit reset establishes this test's zero.
    assert(state(motor).positionCounts==0 && state(motor).bounds==0);
    finishTelemetry(); Serial.output.clear();
    const double physicalHome=physical[axis];
    assert(MotorControl::setDirection(Direction::Counterclockwise));
    assert(outputs[pairs[axis].dir]==LOW && MotorControl::rotateRevolutions(.1f));
    assert(state(motor).remainingCounts==410 && state(motor).remaining>0);
    advanceLoop(2500);
    const auto current=state(motor);
    assert(!current.rotating && current.fault==Fault::None);
    assert(std::fabs(current.position-.1f)<=current.tolerance && current.positionCounts>0);
    assert(outputs[pairs[axis].dir]==LOW && physical[axis]<physicalHome);
    assert(Encoders::get(axis).counts==-current.positionCounts);
    assert(std::fabs(physicalHome-physical[axis]-current.positionCounts)<=.5);
    bool moving=false, livePosition=false;
    for(const auto &s:samples()) if(s.motor==names[axis] && s.mode=="moving" && s.counts>10) {
      assert(s.direction=="ccw" && s.remaining>=0 && s.speed>0); moving=true;
    }
    for(const auto &s:limitSamples()) if(s.motor==names[axis] && s.counts>10) {
      assert(s.lower==-1000000000 && s.upper==1000000000 && s.bounds==0); livePosition=true;
    }
    assert(moving && livePosition);
    Serial.output.clear(); printEncoders();
    bool diagnostic=false;
    for(const auto &line:completeLines(Serial.output)) if(line.find(std::string("ENC ")+names[axis]+" ")==0) {
      assert(line.find(std::string(" counts=")+std::to_string(current.positionCounts)+" ")!=std::string::npos);
      assert(line.find(std::string(" raw=")+std::to_string(Encoders::get(axis).raw)+" ")!=std::string::npos);
      diagnostic=true;
    }
    assert(diagnostic);
    // Manual capture follows the public sign: a positive coordinate saves the upper endpoint.
    assert(MotorControl::setLimit() && state(motor).upperCounts==current.positionCounts);
    assert(state(motor).lowerCounts==-1000000000 && state(motor).bounds==2);
    for(uint8_t other=0;other<3;++other) if(other!=axis) assert(pulseCount[other]==0);
  }
}
void testHeldRampReferenceAndRepeatedPresses() {
  for(uint8_t axis=0;axis<3;++axis) for(Direction direction: {Direction::Clockwise,Direction::Counterclockwise}) {
    reset(); const Motor motor=static_cast<Motor>(axis);
    assert(MotorControl::selectMotor(motor) && MotorControl::setDirection(direction));
    assert(MotorControl::setSpeed(.5f) && MotorControl::setAcceleration(.25f));
    auto &control=MotorControl::motors[axis];
    const double sign=direction==Direction::Clockwise ? 1 : -1;
    for(int press=0;press<2;++press) {
      assert(MotorControl::startHold());
      assert(control.referenceSpeed==0 && control.outputSpeed==0);
      const auto started=control.lastControlMs;
      const auto origin=control.targetCounts; // Reference start; the guard anchor moves back on wake.
      float previousOutput=0; auto previousFeedback=started;
      for(int ms=0;ms<2500;++ms) {
        advance(1);
        if(ms%100==0) assert(MotorControl::keepHoldAlive());
        assert(state(motor).rotating && state(motor).fault==Fault::None);
        const double seconds=(control.lastControlMs-started)*.001;
        const double expected=seconds<2 ? .125*seconds*seconds : .5*(seconds-1);
        const double reference=sign*(control.targetCounts-origin+control.phaseRemainder)/4096.0;
        assert(std::fabs(reference-expected)<.0001); // Analytic area under a two-second speed ramp.
        assert(control.referenceSpeed>=0 && control.referenceSpeed<=.500001);
        const double dt=(control.lastControlMs-previousFeedback)*.001;
        assert(std::fabs(control.outputSpeed-previousOutput)<=.25*dt+.000002);
        assert(sign*control.outputSpeed>=0 && std::fabs(control.outputSpeed)<=.625001);
        previousOutput=control.outputSpeed; previousFeedback=control.lastControlMs;
      }
      assert(control.referenceSpeed>.4999);
      MotorControl::stopRotation();
      const long stopped=pulseCount[axis]; advance(10);
      assert(pulseCount[axis]==stopped && control.referenceSpeed==0 && control.outputSpeed==0);
      assert(!MotorControl::keepHoldAlive());
    }
  }
}
void testHeldRampAccelerationSensitivePlantAndLegacyStart() {
  reset(); accelerationSensitive[0]=true; lastPlantUs[0]=clockUs;
  assert(MotorControl::setSpeed(.5f) && MotorControl::startHold());
  for(int i=0;i<40;++i) { advance(100); assert(MotorControl::keepHoldAlive()); }
  assert(state().rotating && state().fault==Fault::None && accelerationDrops[0]==0);
  // 2s acceleration plus2s cruising:1.5 rotations, with finite polling/correction error.
  assert(std::fabs(state().position+1.5f)<.035);
  reset(); accelerationSensitive[0]=true; lastPlantUs[0]=clockUs;
  assert(MotorControl::setSpeed(.5f) && MotorControl::startRotation());
  advance(20);
  assert(std::fabs(MotorControl::motors[0].outputSpeed)>.49);
  assert(MotorControl::motors[0].referenceSpeed==0); // Legacy start is intentionally not ramped.
  advance(650);
  assert(accelerationDrops[0]>100 && !state().rotating && state().fault==Fault::Tracking);
}
void testHeldRampCorrectionHeadroomAndImmediateStops() {
  reset(); Serial.input="speed 0.5\nhold\n"; advanceLoop(250);
  assert(state().rotating && state().position<0 && state().position>-.02);
  assert(MotorControl::motors[0].referenceSpeed>.05 && MotorControl::motors[0].referenceSpeed<.07);
  assert(Serial.output.find("OK: bottom hold\r\n")!=std::string::npos);
  Serial.input="keepalive\nstop\n"; advanceLoop(10);
  assert(!state().rotating && MotorControl::motors[0].referenceSpeed==0);
  assert(Serial.output.find("OK: bottom keepalive\r\n")!=std::string::npos);
  assert(Serial.output.find("OK: bottom stop\r\n")!=std::string::npos);
  reset(); dropEvery[0]=6;
  assert(MotorControl::setSpeed(.5f) && MotorControl::startHold());
  auto &control=MotorControl::motors[0];
  float previous=0, peak=0; auto previousFeedback=control.lastControlMs;
  for(int ms=0;ms<7000;++ms) {
    if(ms==4500) dropEvery[0]=0;
    advance(1);
    if(ms%100==0) assert(MotorControl::keepHoldAlive());
    assert(state().rotating && state().fault==Fault::None);
    const double dt=(control.lastControlMs-previousFeedback)*.001;
    assert(std::fabs(control.outputSpeed-previous)<=.25*dt+.000002);
    peak=std::max(peak,std::fabs(control.outputSpeed));
    assert(std::fabs(control.outputSpeed)<=.625001);
    previous=control.outputSpeed; previousFeedback=control.lastControlMs;
  }
  assert(peak>.55 && std::fabs(control.outputSpeed)<peak-.01); // Catch-up keeps headroom and slews back down.
  for(int stopKind=0;stopKind<4;++stopKind) {
    reset(); assert(MotorControl::setSpeed(.5f) && MotorControl::setAcceleration(.1f));
    assert(MotorControl::startHold()); advance(250);
    assert(MotorControl::motors[0].referenceSpeed<.03); // All stops happen well before ramp completion.
    if(stopKind==0) MotorControl::stopRotation();
    else if(stopKind==1) MotorControl::stopAll();
    else if(stopKind==2) { analogInputs[0]=250; advance(2); }
    else advance(800); // No heartbeat: the existing one-second watchdog still applies.
    assert(!state().rotating && state().fault==Fault::None);
    assert(MotorControl::motors[0].referenceSpeed==0 && MotorControl::motors[0].outputSpeed==0);
    const long stopped=pulseCount[0]; advance(50); assert(pulseCount[0]==stopped);
  }
  // Physical startup already close to a software bound remains inside it during the ramp.
  reset(); captureTestBounds(-12,12);
  checkPhysicalBounds[0]=true; testLower[0]=4080-12; testUpper[0]=4080+12;
  assert(MotorControl::setSpeed(.5f) && MotorControl::startHold());
  for(int i=0;i<100 && state().rotating;++i) {
    advance(10); if(i%10==0 && state().rotating) assert(MotorControl::keepHoldAlive());
  }
  assert(!state().rotating && state().fault==Fault::None);
}
void plantForCalibration(uint8_t axis,double upper=3480,double lower=4480) {
  physicalUpper[axis]=upper; physicalLower[axis]=lower; switchPlant[axis]=true;
}
void finishCalibration(Motor motor,unsigned long limit=15000) {
  for(unsigned long ms=0;ms<limit && state(motor).rotating;ms+=10) advanceLoop(10);
  if(state(motor).calibration!=Calibration::Done)
    std::cerr<<"calibration failure axis="<<int(motor)<<" phase="<<int(state(motor).calibration)
             <<" fault="<<int(state(motor).fault)<<" pos="<<state(motor).positionCounts<<"\n";
  assert(!state(motor).rotating && state(motor).calibration==Calibration::Done && state(motor).fault==Fault::None);
}
void testAutonomousCalibrationAllAxesAndSavedSettings() {
  const char *names[]={"bottom","top","insert"};
  for(uint8_t axis=0;axis<3;++axis) for(Direction saved: {Direction::Clockwise,Direction::Counterclockwise}) {
    reset(); const Motor motor=static_cast<Motor>(axis);
    assert(MotorControl::selectMotor(motor) && MotorControl::setDirection(saved));
    manualPosition(axis,-100); assert(MotorControl::setLimit());
    manualPosition(axis,100); assert(MotorControl::setLimit()); manualPosition(axis,0);
    assert(MotorControl::setSpeed(.5f)); plantForCalibration(axis);
    assert(MotorControl::calibrate()); assert(state(motor).bounds==0);
    assert(!MotorControl::setDirection(saved) && !MotorControl::startHold() && !MotorControl::startRotation());
    assert(!MotorControl::setSpeed(.1f) && !MotorControl::resetPosition() && !MotorControl::setMicrostep(8));
    assert(MotorControl::selectMotor(static_cast<Motor>((axis+1)%3)));
    const Motor selected=MotorControl::selectedMotor();
    finishCalibration(motor);
    assert(MotorControl::selectedMotor()==selected);
    const auto s=state(motor);
    assert(s.lowerCounts==0 && s.bounds==3 && std::labs(s.upperCounts-1000)<=8);
    assert(std::labs(s.positionCounts-(s.upperCounts+1)/2)<=4);
    assert(s.direction==saved && s.speed==.5f && outputs[pairs[axis].dir]==(saved==Direction::Clockwise ? HIGH:LOW));
    bool lower=false, upper=false, center=false, done=false;
    for(const auto &sample:limitSamples()) if(sample.motor==names[axis]) {
      lower|=sample.calibration=="lower"; upper|=sample.calibration=="upper";
      center|=sample.calibration=="center"; done|=sample.calibration=="done";
    }
    advanceLoop(150);
    for(const auto &sample:limitSamples()) if(sample.motor==names[axis] && sample.calibration=="done") done=true;
    assert(lower && upper && center && done);
    for(uint8_t other=0;other<3;++other) if(other!=axis) assert(pulseCount[other]==0);
    assert(MotorControl::selectMotor(motor)); assert(MotorControl::clearUpperLimit());
    assert(state(motor).calibration==Calibration::None);
  }
  // Three independent state machines transition without changing the global serial selection.
  reset();
  for(uint8_t axis=0;axis<3;++axis) {
    plantForCalibration(axis,3480-100*axis,4480+100*axis);
    assert(MotorControl::selectMotor(static_cast<Motor>(axis)) && MotorControl::calibrate());
  }
  assert(MotorControl::selectMotor(Motor::Top_Motor)); advanceLoop(15000);
  assert(MotorControl::selectedMotor()==Motor::Top_Motor);
  for(uint8_t axis=0;axis<3;++axis) assert(state(static_cast<Motor>(axis)).calibration==Calibration::Done);
}
void testNormalContactsAfterCalibrationPreserveOriginAndBounds() {
  for(uint8_t axis=0;axis<3;++axis) {
    reset(); const Motor motor=static_cast<Motor>(axis);
    assert(MotorControl::selectMotor(motor)); plantForCalibration(axis);
    assert(MotorControl::calibrate()); finishCalibration(motor);
    const long upper=state(motor).upperCounts;
    const double origin=physical[axis]-Encoders::get(axis).counts;
    switchPlant[axis]=false; // Independently actuate switches during hand-driven encoder movement.
    for(uint8_t mask: {1,2}) {
      const long position=mask==1 ? 10 : upper-10;
      manualPosition(axis,-position); analogInputs[axis]=mask==1 ? 250 : 0; advance(25);
      const long before=pulseCount[axis];
      assert(state(motor).positionCounts==position && state(motor).switchMask==mask);
      assert(state(motor).lowerCounts==0 && state(motor).upperCounts==upper && state(motor).bounds==3);
      assert(state(motor).calibration==Calibration::Done && state(motor).fault==Fault::None);
      assert(std::fabs(physical[axis]-Encoders::get(axis).counts-origin)<.6 && pulseCount[axis]==before);
      const Direction outward=mask==1 ? Direction::Clockwise : Direction::Counterclockwise;
      const Direction inward=mask==1 ? Direction::Counterclockwise : Direction::Clockwise;
      assert(MotorControl::setDirection(outward) && !MotorControl::startRotation());
      assert(MotorControl::setDirection(inward) && MotorControl::rotateRevolutions(.01f)); finish(30000,motor);
      assert(pulseCount[axis]>before);
      analogInputs[axis]=1023; advance(25);
      assert(MotorControl::setDirection(outward) && MotorControl::rotateRevolutions(.005f)); finish(30000,motor);
      assert(state(motor).lowerCounts==0 && state(motor).upperCounts==upper && state(motor).bounds==3);
      assert(physical[axis]-Encoders::get(axis).counts>origin-.6 &&
             physical[axis]-Encoders::get(axis).counts<origin+.6);
    }
    // A hand displacement beyond lower does not move its saved zero, even after releasing it.
    manualPosition(axis,20); analogInputs[axis]=250; advance(25);
    assert(state(motor).positionCounts==-20 && state(motor).lowerCounts==0 && state(motor).upperCounts==upper);
    analogInputs[axis]=1023; advance(25);
    assert(MotorControl::setDirection(Direction::Clockwise) && !MotorControl::rotateRevolutions(.001f));
    assert(MotorControl::setDirection(Direction::Counterclockwise) && MotorControl::rotateRevolutions(.02f)); finish(30000,motor);
    assert(state(motor).positionCounts>0 && state(motor).calibration==Calibration::Done);
    for(uint8_t other=0;other<3;++other) if(other!=axis) assert(pulseCount[other]==0);
  }
}
void testCalibrationPressedSwitchesOddSpanAndBusyDebounce() {
  for(bool atLower: {false,true}) {
    reset(); plantForCalibration(0,atLower ? 3055:4080,atLower ? 4080:5105);
    MotorControl::begin(); advance(25);
    assert(MotorControl::resetPosition()); // An already-held contact remains usable for explicit calibration.
    assert(MotorControl::calibrate()); finishCalibration(Bottom);
    assert(state().bounds==3 && state().lowerCounts==0 && state().upperCounts>1000);
  }
  reset(); analogInputs[0]=250;
  assert(MotorControl::calibrate());
  assert(state().rotating && state().continuous && MotorControl::motors[0].mode==MotorControl::Mode::Idle);
  assert(!MotorControl::calibrate() && !MotorControl::startRotation() && !MotorControl::startHold());
  assert(!MotorControl::rotateRevolutions(.1f) && !MotorControl::clearFault() && !MotorControl::setLimit());
  advance(25); assert(state().calibration==Calibration::Upper);
  analogInputs[0]=0; delayMicroseconds(1000); MotorControl::update(); // Stop before seeding the exact odd span.
  manualPosition(0,-1025); advance(25);
  assert(state().calibration==Calibration::Center && state().upperCounts==1025);
  assert(MotorControl::motors[0].targetCounts==-513);
  finishCalibration(Bottom); assert(std::labs(state().positionCounts-513)<=4);
  assert(MotorControl::resetPosition() && state().calibration==Calibration::None && state().bounds==0);
  // Two interlocks during an analog transition cannot start calibration or erase old bounds.
  reset(); captureTestBounds(-100,100); analogInputs[0]=0; advance(25);
  const auto before=state(); analogInputs[0]=250; advance(1);
  assert(state().switchMask==3 && !MotorControl::calibrate());
  assert(state().bounds==before.bounds && state().lowerCounts==before.lowerCounts && state().upperCounts==before.upperCounts);
}
void testCalibrationCancellationTimeoutAndFaults() {
  for(Calibration phase: {Calibration::Lower,Calibration::Upper,Calibration::Center}) for(bool all: {false,true}) {
    reset(); plantForCalibration(0); assert(MotorControl::setDirection(Direction::Counterclockwise));
    assert(MotorControl::calibrate());
    for(int ms=0;ms<12000 && state().calibration!=phase;ms+=10) advance(10);
    assert(state().calibration==phase && state().rotating);
    const auto bounds=state().bounds;
    if(all) MotorControl::stopAll(); else MotorControl::stopRotation();
    const long stopped=pulseCount[0]; advance(100);
    assert(!state().rotating && state().calibration==Calibration::Cancelled && pulseCount[0]==stopped);
    assert(state().fault==Fault::None && state().direction==Direction::Counterclockwise && state().bounds==bounds);
    assert(!MotorControl::keepHoldAlive());
  }
  // False candidate contact never resumes movement while calibration waits for confirmation.
  reset(); assert(MotorControl::calibrate()); advance(100);
  analogInputs[0]=250; advance(2); const long stopped=pulseCount[0];
  analogInputs[0]=1023; advance(50);
  assert(state().rotating && state().calibration==Calibration::Lower && pulseCount[0]==stopped);
  assert(MotorControl::motors[0].mode==MotorControl::Mode::Idle);
  MotorControl::motors[0].phaseStartedMs=millis()-EncoderConfig::CalibrationSearchTimeoutMs;
  advance(1); assert(state().fault==Fault::Timeout && state().calibration==Calibration::Failed);
  // A missing physical switch is bounded even when ordinary motion is healthy and progressing.
  reset(); assert(MotorControl::setSpeed(.05f) && MotorControl::calibrate());
  advance(EncoderConfig::CalibrationSearchTimeoutMs+10);
  assert(!state().rotating && state().fault==Fault::Timeout && state().calibration==Calibration::Failed);
  for(int failure=0;failure<3;++failure) {
    reset(); plantForCalibration(0); assert(MotorControl::calibrate()); advance(100);
    if(failure==0) online[0]=false;
    else if(failure==1) jammed[0]=true;
    else { physical[0]-=100; }
    advance(3000);
    assert(!state().rotating && state().calibration==Calibration::Failed && state().fault!=Fault::None);
    const long pulses=pulseCount[0]; advance(100); assert(pulseCount[0]==pulses);
  }
  // Feedback loss while internally idle at a candidate contact must fail promptly.
  reset(); analogInputs[0]=250; assert(MotorControl::calibrate()); online[0]=false; advance(15);
  assert(state().calibration==Calibration::Failed && state().fault==Fault::Encoder);
  // A confirmed upper at the same measured position as lower is not a valid travel span.
  reset(); analogInputs[0]=250; assert(MotorControl::calibrate()); advance(25);
  analogInputs[0]=0; advance(25);
  assert(state().calibration==Calibration::Failed && state().fault==Fault::Tracking);
}
void testAbsolutePositionsAllAxesAndDirections() {
  for(uint8_t axis=0;axis<3;++axis) for(Direction configured: {Direction::Clockwise,Direction::Counterclockwise}) {
    reset(); const Motor motor=static_cast<Motor>(axis);
    assert(MotorControl::selectMotor(motor) && MotorControl::setDirection(configured));
    plantForCalibration(axis); assert(MotorControl::calibrate()); finishCalibration(motor);
    const long upper=state(motor).upperCounts;
    for(long target: {164L,819L,0L,upper}) {
      const long current=state(motor).positionCounts;
      assert(MotorControl::goToPosition(target/4096.0f));
      assert(state(motor).remainingCounts==target-current && state(motor).rotating);
      const Direction moving=target>current ? Direction::Counterclockwise : Direction::Clockwise;
      assert(state(motor).direction==moving);
      advance(80); assert(outputs[pairs[axis].dir]==(moving==Direction::Clockwise ? HIGH:LOW));
      finish(30000,motor);
      assert(std::labs(state(motor).positionCounts-target)<=4);
      assert(state(motor).direction==configured && outputs[pairs[axis].dir]==(configured==Direction::Clockwise ? HIGH:LOW));
      assert(state(motor).calibration==Calibration::Done && state(motor).lowerCounts==0 && state(motor).upperCounts==upper);
      const long before=pulseCount[axis];
      assert(MotorControl::goToPosition(target/4096.0f)); advance(25);
      assert(!state(motor).rotating && pulseCount[axis]==before);
    }
    assert(!MotorControl::goToPosition(-.25f/4096) && !MotorControl::goToPosition((upper+.25f)/4096));
    switchPlant[axis]=false;
    manualPosition(axis,-164); // A sub-count target inside completion tolerance needs no pulse.
    analogInputs[axis]=1023; advance(25);
    const long before=pulseCount[axis];
    assert(MotorControl::goToPosition(164.49f/4096));
    assert(!state(motor).rotating && pulseCount[axis]==before && state(motor).direction==configured);
    for(uint8_t other=0;other<3;++other) if(other!=axis) assert(pulseCount[other]==0);
  }
}
void testAbsoluteEligibilityAndSignedSerialParsing() {
  reset(); assert(!MotorControl::goToPosition(0)); captureTestBounds(-1000,1000);
  assert(!MotorControl::goToPosition(.1f)); // Manually saved bounds are not successful calibration.
  assert(MotorControl::calibrate() && !MotorControl::goToPosition(0));
  MotorControl::stopRotation(); assert(state().calibration==Calibration::Cancelled && !MotorControl::goToPosition(0));
  assert(MotorControl::calibrate()); online[0]=false; advance(20);
  assert(state().calibration==Calibration::Failed && !MotorControl::goToPosition(0));
  for(int command=0;command<4;++command) {
    reset(); plantForCalibration(0); assert(MotorControl::calibrate()); finishCalibration(Bottom);
    assert(command==0 ? MotorControl::resetPosition() : command==1 ? MotorControl::setLimit() :
           command==2 ? MotorControl::clearLowerLimit() : MotorControl::clearUpperLimit());
    assert(state().calibration==Calibration::None && !MotorControl::goToPosition(0));
  }
  reset(); plantForCalibration(0); assert(MotorControl::calibrate()); finishCalibration(Bottom);
  switchPlant[0]=false; manualPosition(0,0); analogInputs[0]=250; advance(25);
  const long before=pulseCount[0];
  assert(MotorControl::goToPosition(0) && MotorControl::goToPosition(1.0f/4096));
  assert(!state().rotating && pulseCount[0]==before && state().switchMask==1);
  assert(!MotorControl::goToPosition(NAN) && !MotorControl::goToPosition(INFINITY));
  Serial.output.clear();
  Serial.input="goto\ngoto nan\ngoto inf\ngoto -0.00001\ngoto 1e999\ngoto junk\ngoto 0x0p0\ngoto 0 extra\ngoto +0.0\ngoto -0\n";
  advanceLoop(100);
  int errors=0, acknowledged=0;
  for(const auto &line:completeLines(Serial.output)) {
    errors+=line.find("ERROR:")==0;
    acknowledged+=line=="OK: bottom goto";
  }
  assert(errors==8 && acknowledged==2 && pulseCount[0]==before && state().positionCounts==0);
  analogInputs[0]=1023; advance(25);
  Serial.input="goto +4e-2\n"; advanceLoop(1500);
  assert(state().fault==Fault::None && !state().rotating && std::labs(state().positionCounts-164)<=4);
  assert(state().calibration==Calibration::Done);
  // Zero-distance targets still require fresh trusted feedback, including an idle disconnect.
  online[0]=false; assert(!MotorControl::goToPosition(state().position)); online[0]=true;
  assert(!MotorControl::clearFault() && !MotorControl::goToPosition(state().position));
}
void testAbsoluteCancellationFaultsAndSelectionIsolation() {
  for(int interruption=0;interruption<3;++interruption) {
    reset(); plantForCalibration(0); assert(MotorControl::calibrate()); finishCalibration(Bottom);
    const long upper=state().upperCounts;
    assert(MotorControl::setDirection(Direction::Counterclockwise));
    assert(MotorControl::goToPosition(.04f)); advance(100);
    assert(!MotorControl::goToPosition(.1f) && !MotorControl::setSpeed(.1f));
    if(interruption==0) MotorControl::stopRotation();
    else if(interruption==1) MotorControl::stopAll();
    else { switchPlant[0]=false; analogInputs[0]=250; advance(2); }
    assert(!state().rotating && state().direction==Direction::Counterclockwise && state().calibration==Calibration::Done);
    const long stopped=pulseCount[0]; advance(30);
    assert(pulseCount[0]==stopped && state().lowerCounts==0 && state().upperCounts==upper);
    analogInputs[0]=1023; advance(25);
    assert(MotorControl::goToPosition(.04f)); finish();
    assert(std::labs(state().positionCounts-164)<=4 && state().calibration==Calibration::Done);
  }
  reset(); plantForCalibration(0); assert(MotorControl::calibrate()); finishCalibration(Bottom);
  jammed[0]=true; assert(MotorControl::setDirection(Direction::Counterclockwise));
  assert(MotorControl::goToPosition(.04f)); advance(2500);
  assert(state().fault==Fault::Stall && !state().rotating && state().calibration==Calibration::Done);
  assert(state().direction==Direction::Counterclockwise && !MotorControl::goToPosition(.04f));
  jammed[0]=false; assert(MotorControl::clearFault() && MotorControl::goToPosition(.04f)); finish();
  assert(std::labs(state().positionCounts-164)<=4);
  // Calibrations and subsequent absolute moves remain independent of the global selected axis.
  reset();
  for(uint8_t axis=0;axis<3;++axis) {
    plantForCalibration(axis); assert(MotorControl::selectMotor(static_cast<Motor>(axis)) && MotorControl::calibrate());
  }
  advanceLoop(15000);
  for(uint8_t axis=0;axis<3;++axis) {
    assert(MotorControl::selectMotor(static_cast<Motor>(axis)) && MotorControl::goToPosition(.04f+.04f*axis));
  }
  assert(MotorControl::selectMotor(Motor::Top_Motor)); advanceLoop(2500);
  assert(MotorControl::selectedMotor()==Motor::Top_Motor);
  for(uint8_t axis=0;axis<3;++axis) {
    const auto s=state(static_cast<Motor>(axis));
    assert(!s.rotating && s.fault==Fault::None && s.calibration==Calibration::Done);
    assert(std::fabs(s.position-(.04f+.04f*axis))<=5.0f/4096);
  }
}
void injectHeldReverse(uint8_t axis,long counts) {
  auto &m=MotorControl::motors[axis];
  const long sign=m.direction==Direction::Clockwise ? 1 : -1;
  manualPosition(axis,m.directionOriginCounts-sign*counts);
  advance(15);
}
void testHeldDirectionPauseRecoveryAndSlowSpeed() {
  for(uint8_t axis=0;axis<3;++axis) for(Direction dir: {Direction::Clockwise,Direction::Counterclockwise})
    for(float speed: {.001f,.25f}) {
      reset(); const Motor motor=static_cast<Motor>(axis);
      assert(MotorControl::selectMotor(motor) && MotorControl::setDirection(dir) && MotorControl::setSpeed(speed));
      manualPosition(axis,-1000); assert(MotorControl::setLimit());
      manualPosition(axis,1000); assert(MotorControl::setLimit()); manualPosition(axis,0);
      assert(MotorControl::startHold()); injectHeldReverse(axis,40);
      auto &control=MotorControl::motors[axis]; const long hardAnchor=control.originCounts;
      const long frozenTarget=control.targetCounts, before=pulseCount[axis];
      assert(state(motor).directionPaused && state(motor).directionWarnings==1 && state(motor).fault==Fault::None);
      assert(state(motor).rotating && state(motor).continuous && MotorControl::keepHoldAlive());
      assert(control.outputSpeed==0 && control.referenceSpeed==0 && requestedPulseRate(axis)==0);
      advance(70);
      assert(state(motor).directionPaused && pulseCount[axis]==before && control.targetCounts==frozenTarget);
      for(int i=0;i<80 && state(motor).directionPaused;++i) advance(1);
      assert(!state(motor).directionPaused && state(motor).rotating && state(motor).fault==Fault::None);
      assert(control.outputSpeed==0 && control.referenceSpeed==0 && control.phaseRemainder==0);
      assert(control.targetCounts==Encoders::get(axis).counts && control.originCounts==hardAnchor);
      assert(control.samplePulses==control.progressPulses && control.lastControlMs==Encoders::get(axis).sampledAt);
      const long retryOrigin=Encoders::get(axis).counts;
      for(int i=0;i<20;++i) { advance(100); assert(MotorControl::keepHoldAlive()); }
      assert(state(motor).fault==Fault::None && state(motor).directionWarnings==1 && pulseCount[axis]>before);
      assert(dir==Direction::Clockwise ? Encoders::get(axis).counts>retryOrigin : Encoders::get(axis).counts<retryOrigin);
      assert(state(motor).lowerCounts==-1000 && state(motor).upperCounts==1000 && state(motor).bounds==3);
      for(uint8_t other=0;other<3;++other) if(other!=axis)
        assert(state(static_cast<Motor>(other)).directionWarnings==0 && pulseCount[other]==0);
      MotorControl::stopRotation(); assert(state(motor).directionWarnings==1 && !state(motor).directionPaused);
    }
  // A one-sample spike may return to the original coordinate before the stable retry.
  reset(); assert(MotorControl::startHold());
  const long startAnchor=MotorControl::motors[0].originCounts; injectHeldReverse(0,40);
  manualPosition(0,0); advance(140);
  assert(!state().directionPaused && state().fault==Fault::None && state().directionWarnings==1);
  assert(MotorControl::motors[0].originCounts==startAnchor && state().bounds==0);
}
void testHeldDirectionHardBudgetFrequencyAndInstability() {
  for(long amount: {32L,33L,63L,64L,100L}) {
    reset(); assert(MotorControl::startHold()); injectHeldReverse(0,amount);
    assert(state().directionWarnings==(amount>32 ? 1 : 0));
    assert(state().directionPaused==(amount>32 && amount<64));
    assert(state().fault==(amount>=64 ? Fault::Direction : Fault::None));
  }
  reset(); assert(MotorControl::startHold()); injectHeldReverse(0,40);
  advance(130); assert(!state().directionPaused);
  manualPosition(0,MotorControl::motors[0].originCounts-64); advance(15); // Only24 further reverse counts: fixed original anchor still faults.
  assert(state().fault==Fault::Direction && state().directionWarnings==2 && !state().directionPaused);
  reset(); assert(MotorControl::startHold()); injectHeldReverse(0,40);
  manualPosition(0,MotorControl::motors[0].originCounts-64); advance(15); // Same still-paused episode must not increment again.
  assert(state().fault==Fault::Direction && state().directionWarnings==1);
  reset(); assert(MotorControl::startHold()); injectHeldReverse(0,40);
  for(int i=0;i<10 && state().rotating;++i) {
    manualPosition(0,MotorControl::motors[0].originCounts-(i%2 ? 40 : 48)); advance(60);
    if(state().rotating) assert(MotorControl::keepHoldAlive());
  }
  assert(!state().rotating && state().fault==Fault::Direction && state().directionWarnings==1);
  // Three episodes in a true sliding window, crossing a hypothetical fixed10s bucket.
  reset();
  for(int episode=0;episode<3;++episode) {
    assert(MotorControl::startHold()); injectHeldReverse(0,40);
    assert(state().directionWarnings==episode+1);
    if(episode==2) { assert(state().fault==Fault::Direction && !state().directionPaused); break; }
    assert(state().directionPaused); MotorControl::stopRotation();
    advance(episode==0 ? 8000 : 1500);
  }
  // With real reversed feedback, retry cannot hide accumulating backwards motion at either speed.
  for(float speed: {.001f,.25f}) {
    reset(); inverted[0]=true; MotorControl::begin();
    assert(MotorControl::setSpeed(speed) && MotorControl::startHold());
    for(int i=0;i<300 && state().rotating;++i) {
      advance(100); if(state().rotating) assert(MotorControl::keepHoldAlive());
    }
    assert(!state().rotating && state().fault==Fault::Direction);
    // Measured from the hard anchor, which the wake moved back by the snap allowance.
    const long reverse=std::labs(Encoders::get(0).counts-MotorControl::motors[0].originCounts);
    assert(state().directionWarnings>=1 && reverse>=64 && reverse<96);
  }
}
void testHeldDirectionCancellationAndStrictOtherModes() {
  for(uint8_t axis=0;axis<3;++axis) for(int stopKind=0;stopKind<5;++stopKind) {
    reset(); const Motor motor=static_cast<Motor>(axis);
    assert(MotorControl::selectMotor(motor));
    if(stopKind==3) { assert(MotorControl::startHold()); advance(950); }
    else assert(MotorControl::startHold());
    injectHeldReverse(axis,40); assert(state(motor).directionPaused);
    const long before=pulseCount[axis];
    if(stopKind==0) MotorControl::stopRotation();
    else if(stopKind==1) MotorControl::stopAll();
    else if(stopKind==2) analogInputs[axis]=250;
    else if(stopKind==4) online[axis]=false;
    advance(160);
    assert(!state(motor).rotating && !state(motor).directionPaused && state(motor).directionWarnings==1);
    assert(pulseCount[axis]==before && !MotorControl::keepHoldAlive());
    assert(state(motor).fault==(stopKind==4 ? Fault::Encoder : Fault::None));
  }
  // Finite, legacy start, automatic calibration, and absolute positioning stay strict at33 counts.
  for(int mode=0;mode<4;++mode) {
    reset();
    if(mode==0) assert(MotorControl::rotateRevolutions(.1f));
    if(mode==1) assert(MotorControl::startRotation());
    if(mode==2) assert(MotorControl::calibrate());
    if(mode==3) {
      plantForCalibration(0); assert(MotorControl::calibrate()); finishCalibration(Bottom);
      assert(MotorControl::goToPosition(.04f));
    }
    auto &m=MotorControl::motors[0];
    const long sign=m.direction==Direction::Clockwise ? 1 : -1;
    manualPosition(0,m.originCounts-sign*40); advance(15);
    assert(state().fault==Fault::Direction && !state().rotating && state().directionWarnings==0);
  }
}
void testHeldDirectionHistoryAndCalibrationReset() {
  reset();
  for(unsigned int episode=1;episode<=6;++episode) {
    assert(MotorControl::startHold()); injectHeldReverse(0,40);
    assert(state().directionWarnings==episode && state().fault==Fault::None);
    MotorControl::stopRotation();
    assert(MotorControl::clearFault() && MotorControl::resetPosition());
    assert(MotorControl::clearLowerLimit() && MotorControl::clearUpperLimit());
    assert(state().directionWarnings==episode && state().bounds==0);
    advance(10100);
  }
  assert(state().directionWarnings>5 && state().calibration==Calibration::None);
  plantForCalibration(0); assert(MotorControl::calibrate()); advance(30); MotorControl::stopRotation();
  assert(state().directionWarnings==6 && state().calibration==Calibration::Cancelled);
  assert(MotorControl::calibrate()); online[0]=false; advance(15);
  assert(state().directionWarnings==6 && state().calibration==Calibration::Failed);
  online[0]=true; advance(20); assert(MotorControl::clearFault());
  assert(MotorControl::calibrate()); finishCalibration(Bottom);
  assert(state().directionWarnings==0 && state().calibration==Calibration::Done);
  MotorControl::motors[0].directionWarnings=65535; // Saturation boundary, without65535 physical episodes.
  switchPlant[0]=false; assert(MotorControl::clearLowerLimit() && MotorControl::clearUpperLimit());
  advance(10100); assert(MotorControl::startHold()); injectHeldReverse(0,40);
  assert(state().directionWarnings==65535);
  reset(); assert(state().directionWarnings==0);
  clockUs=(uint64_t(UINT32_MAX)-50)*1000; MotorControl::begin();
  assert(MotorControl::startHold()); injectHeldReverse(0,40); advance(140);
  assert(state().fault==Fault::None && state().directionWarnings==1 && !state().directionPaused);
  MotorControl::stopRotation();
  for(int i=0;i<2;++i) {
    assert(MotorControl::startHold()); injectHeldReverse(0,40); MotorControl::stopRotation();
  }
  assert(state().fault==Fault::Direction && state().directionWarnings==3);
  // Old lifetime history cannot become a recent three-event window after49.7days.
  reset();
  for(int i=0;i<2;++i) {
    assert(MotorControl::startHold()); injectHeldReverse(0,40); MotorControl::stopRotation(); advance(1000);
  }
  for(uint64_t time: {uint64_t(20000),uint64_t(UINT32_MAX/2),uint64_t(UINT32_MAX)-1000,
                      uint64_t(UINT32_MAX)+3000}) {
    clockUs=time*1000; MotorControl::update();
  }
  assert(MotorControl::clearFault() && state().directionWarnings==2);
  assert(MotorControl::startHold()); injectHeldReverse(0,40);
  assert(state().directionWarnings==3 && state().directionPaused && state().fault==Fault::None);
}
void testHeldWarningTelemetryAndSharedFraming() {
  reset(); printStatus();
  assert(Serial.output.find("warnings=held_direction_v1\r\npositioning=calibrated_absolute_v1")==0);
  Serial.input="hold\n"; advanceLoop(20); injectHeldReverse(0,40); advanceLoop(70);
  printStatus();
  assert(Serial.output.find("warning=direction_pause warnings=1")!=std::string::npos);
  advanceLoop(150); Serial.input="stop\n"; advanceLoop(150); finishTelemetry();
  bool paused=false,resumed=false,stopped=false;
  for(const auto &s:limitSamples()) if(s.motor=="bottom" && s.warnings==1) {
    assert(s.bounds==0 && s.lower==-1000000000 && s.upper==1000000000);
    paused|=s.warning=="direction_pause"; resumed|=s.warning=="none";
  }
  for(const auto &s:samples()) if(s.motor=="bottom" && s.mode=="idle" && s.counts!=0) stopped=true;
  assert(paused && resumed && stopped && state().directionWarnings==1 && !state().directionPaused);
  assert(Serial.output.find("OK: bottom hold")!=std::string::npos && Serial.output.find("OK: bottom stop")!=std::string::npos);
  // Long counter/sequence fields still fit the unchanged fixed UART buffer.
  reset(); telemetrySequence=UINT32_MAX-2; clockUs=(uint64_t(UINT32_MAX)-30)*1000;
  MotorControl::motors[0].directionWarnings=65535;
  MotorControl::motors[0].directionPauseSamples=1;
  MotorControl::motors[0].calibration=Calibration::Cancelled;
  telemetryLimits=true; telemetryAxis=0; serviceTelemetry(); finishTelemetry();
  const auto maximum=limitSamples();
  assert(maximum.size()==1 && maximum[0].warnings==65535 && maximum[0].warning=="direction_pause");
  assert(!telemetryLine.overflow && Serial.output.size()<sizeof(telemetryLine.data));
}
void testBoundedSerialBuffersAndRecovery() {
  // Deliberately combine every field's maximum width, even where not physically simultaneous.
  const std::string worstTel="TEL 4294967295 4294967295 insert ccw continuous counts=-1000000000 remaining=-2000000000 speed=-500.000000 encoder=untrusted fault=direction raw=4095 channel=7 magnet=255 age=4294967295\r\n";
  const std::string worstLim="LIM 4294967295 4294967295 insert lower=-1000000000 upper=1000000000 bounds=3 switch=3 raw=1023 counts=-1000000000 calibration=cancelled warning=direction_pause warnings=65535\r\n";
  assert(worstTel.size()==187 && worstLim.size()==176);
  parseTelemetry(worstTel); parseLimits(worstLim);
  for(const auto &line: {worstTel,worstLim}) {
    reset(); telemetryLine.print(line.c_str());
    assert(!telemetryLine.overflow && telemetryLine.length==line.size());
    finishTelemetry(); assert(Serial.output==line);
  }
  reset(); const std::string speed="speed 0.2";
  Serial.input=speed+std::string(63-speed.size(),' ')+"\n"; advanceLoop(20);
  assert(state().speed==.2f && Serial.output.find("OK: bottom speed")!=std::string::npos);
  Serial.output.clear(); Serial.input="rev 1"+std::string(59,' ')+"\nstatus\n"; advanceLoop(50);
  assert(!state().rotating && pulseCount[0]==0 && !discardLine && commandLength==0);
  assert(Serial.output.find("ERROR: Bad/long line; discarded.")!=std::string::npos);
  assert(Serial.output.find("OK: bottom rev")==std::string::npos);
  assert(Serial.output.find("warnings=held_direction_v1")!=std::string::npos);
  Serial.input="rev 0.01\n"; advanceLoop(1500);
  assert(state().fault==Fault::None && !state().rotating && std::labs(state().positionCounts+41)<=4);
}
void emitWarningTrace() {
  reset(); Serial.roomProvider=slowUartRoom; Serial.byteObserver=observeUartByte;
  lastUartByte=clockUs; checkPendingLineBeforeMove=false;
  printStatus(); Serial.input="motor bottom\nspeed 0.001\n"; advanceLoop(100);
  for(unsigned int episode=1;episode<=6;++episode) {
    Serial.input="hold\n"; advanceLoop(30);
    manualPosition(0,MotorControl::motors[0].directionOriginCounts-40);
    advanceLoop(250);
    assert(state().rotating && !state().directionPaused && state().fault==Fault::None && state().directionWarnings==episode);
    Serial.input="stop\n"; advanceLoop(150);
    assert(!state().rotating && state().calibration==Calibration::None && state().bounds==0);
    if(episode<6) advanceLoop(10100);
  }
  advanceLoop(200); finishTelemetry();
  bool sawPause=false;
  for(const auto &s:limitSamples()) if(s.motor=="bottom" && s.warning=="direction_pause") sawPause=true;
  assert(sawPause && state().directionWarnings==6);
#ifdef _WIN32
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  std::cout<<Serial.output;
}
void emitTelemetryTrace() {
  reset();
  Serial.roomProvider=slowUartRoom; Serial.byteObserver=observeUartByte;
  lastUartByte=clockUs; checkPendingLineBeforeMove=false;
  printStatus();
  advanceLoop(250);
  Serial.input="motor bottom\ndir ccw\nspeed 0.2\nrev 0.1\n";
  advanceLoop(2500);
  finishTelemetry();
  assert(!state().rotating && state().fault==Fault::None);
  assert(std::labs(state().positionCounts-410)<=4 && outputs[MotorPins::Bottom.dir]==LOW);
  assert(state(Motor::Top_Motor).positionCounts==0 && state(Motor::Insert_Motor).positionCounts==0);
  manualPosition(1,-300); analogInputs[1]=0; advanceLoop(150);
  analogInputs[1]=1023; advanceLoop(25);
  manualPosition(1,500); advanceLoop(150);
  analogInputs[1]=250; advanceLoop(250); finishTelemetry();
  assert(state(Motor::Top_Motor).lowerCounts==-1000000000 && state(Motor::Top_Motor).bounds==0);
  assert(state(Motor::Top_Motor).upperCounts==1000000000 && state(Motor::Top_Motor).positionCounts==-500);
  assert(state(Motor::Top_Motor).switchMask==1 && pulseCount[1]==0);
#ifdef _WIN32
  // FakeSerial already contains UART CRLF bytes; avoid CRT adding a second CR.
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  std::cout<<Serial.output;
}
void emitCalibrationTrace(bool positioning=false) {
  reset(); plantForCalibration(0);
  Serial.roomProvider=slowUartRoom; Serial.byteObserver=observeUartByte;
  lastUartByte=clockUs; checkPendingLineBeforeMove=false;
  printStatus();
  Serial.input="motor bottom\nspeed 0.2\ncalibrate\n";
  advanceLoop(25); finishCalibration(Bottom); advanceLoop(250); finishTelemetry();
  assert(state().lowerCounts==0 && state().upperCounts>0 && state().calibration==Calibration::Done);
  if(positioning) {
    Serial.input="goto 0.04\n"; advanceLoop(2500);
    assert(!state().rotating && state().fault==Fault::None && std::labs(state().positionCounts-164)<=4);
    Serial.input="goto 0.2\n"; advanceLoop(2500); finishTelemetry();
    assert(!state().rotating && state().fault==Fault::None && std::labs(state().positionCounts-819)<=4);
    assert(state().calibration==Calibration::Done && state().direction==Direction::Clockwise);
  }
#ifdef _WIN32
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  std::cout<<Serial.output;
}
void testBouncingStall() {
  // A blocked hybrid stepper does not freeze: each electrical cycle pulls the rotor
  // back up to 2 full steps, then pushes it into the obstruction again.
  for(bool jitter: {false,true}) for(Direction dir: {Direction::Clockwise,Direction::Counterclockwise}) {
    reset(); noisy[0]=jitter;
    const int side=dir==Direction::Clockwise ? 1 : -1;
    field[0]=physical[0]; wallAt[0]=physical[0]+side*0.5*4096; wallSide[0]=side;
    assert(MotorControl::setDirection(dir)); assert(MotorControl::rotateRevolutions(5));
    unsigned long elapsed=0; double bounce=0;
    while(state().rotating && elapsed<40000) {
      advance(10); elapsed+=10;
      if(side*(field[0]-wallAt[0])>0) bounce=std::max(bounce,side*(wallAt[0]-physical[0]));
    }
    // Contact comes after about 2.5 s. The push must end 1.5 s later, not at the 37 s timeout.
    if(state().fault!=Fault::Stall || elapsed>4500)
      std::cerr<<"bouncing stall fault="<<int(state().fault)<<" elapsed="<<elapsed<<"\n";
    assert(state().fault==Fault::Stall && elapsed<=4500 && bounce>=20);
    assert(side*(physical[0]-wallAt[0])<=1e-6);
  }
}
void testDriverWakeSnap() {
  // SLEEP high returns the A4988 translator to Home. The rotor snaps up to 2 full steps
  // either way, during the wake delay or, held by friction, after the first pulses.
  struct Case { Direction dir; int microsteps; float rotations; };
  const Direction cw=Direction::Clockwise, ccw=Direction::Counterclockwise;
  for(int held: {0,1}) {
    auto sleeping=[held](int microsteps) {
      reset(); homeOnWake[0]=true; translator[0]=microsteps; snapPulses=held;
    };
    // 14/18 snap 1.75 full steps against cw/ccw. 17/15 snap 1.875 full steps past a
    // 0.001-rotation target, so the move corrects backward toward the command origin.
    for(Case c: {Case{cw,14,.1f},Case{ccw,18,.1f},Case{cw,17,.001f},Case{ccw,15,.001f}}) {
      sleeping(c.microsteps);
      assert(MotorControl::setDirection(c.dir));
      const float origin=state().position;
      assert(MotorControl::rotateRevolutions(c.rotations));
      finish();
      const float expected=origin+(c.dir==cw ? -c.rotations : c.rotations);
      assert(std::fabs(state().position-expected)<=state().tolerance+1.0/4096);
      assert(state().directionWarnings==0);
    }
    // A snap against a hold is not a direction warning.
    for(Direction dir: {cw,ccw}) {
      sleeping(dir==cw ? 14 : 18);
      assert(MotorControl::setDirection(dir));
      const float origin=state().position;
      assert(MotorControl::startHold());
      for(int i=0;i<5;++i) { advance(200); assert(MotorControl::keepHoldAlive()); }
      MotorControl::stopAll();
      assert(state().fault==Fault::None && state().directionWarnings==0);
      assert((dir==cw ? origin-state().position : state().position-origin)>.05f);
    }
    // Calibration starts its first seek from a sleeping driver.
    sleeping(14); plantForCalibration(0);
    assert(MotorControl::calibrate()); finishCalibration(Bottom);
    // Each sleep repeats the snap, as in the bench trials: 0.0085 rotations is 1.7 full steps.
    sleeping(0);
    for(int i=0;i<8;++i) {
      advance(10100); assert(outputs[MotorPins::BottomSleep]==LOW);
      finite(.0085f, i<4 ? cw : ccw);
    }
    // Reverse motion beyond the snap allowance still faults.
    sleeping(0);
    assert(MotorControl::rotateRevolutions(.1f)); advance(30);
    manualPosition(0,MotorControl::motors[0].originCounts-40); advance(30);
    assert(!state().rotating && state().fault==Fault::Direction);
  }
}
int main(int argc, char **argv) {
  if(argc==2 && std::strcmp(argv[1],"--trace")==0) { emitTelemetryTrace(); return 0; }
  if(argc==2 && std::strcmp(argv[1],"--calibration-trace")==0) { emitCalibrationTrace(); return 0; }
  if(argc==2 && std::strcmp(argv[1],"--positioning-trace")==0) { emitCalibrationTrace(true); return 0; }
  if(argc==2 && std::strcmp(argv[1],"--warning-trace")==0) { emitWarningTrace(); return 0; }
  if(argc!=1) { std::cerr<<"Usage: native.exe [--trace|--calibration-trace|--positioning-trace|--warning-trace]\n"; return 2; }
  testFractionalAndWrapping(); std::cout<<"fractional multi-turn bidirectional wrapping passed\n";
  testCorrectsLostPulsesAndOvershoot(); std::cout<<"missed pulse and reverse overshoot correction passed\n";
  testFixedEighthAndConcurrentAxes(); std::cout<<"fixed eighth pins, rejection, pulse scale and simultaneous axes passed\n";
  testHoldFeedbackAndWatchdog(); std::cout<<"held velocity feedback, stop and watchdog passed\n";
  testFaultsAndRecovery(); std::cout<<"stall/sign/I2C/magnet/stale faults and explicit recovery passed\n";
  testSlowMovesAndSleep(); std::cout<<"slow movement and driver sleep/wake passed\n";
  testBouncingStall(); std::cout<<"rotor bouncing at an obstruction faults stall in both directions passed\n";
  testDriverWakeSnap(); std::cout<<"driver wake snap before the first pulse: moves, holds, calibration and repeated sleeps passed\n";
  testProtocolAndBounds(); std::cout<<"serial protocol and numerical bounds passed\n";
  testTimerRollover(); std::cout<<"millis/micros rollover passed\n";
  testRegisterReadsAndLiveDiagnostics();
  std::cout<<"explicit register reads, per-channel raw feedback, idle/fault diagnostics and short-read faults passed\n";
  testAutonomousTelemetryAndCommands();
  std::cout<<"autonomous telemetry rate, idle/move/fault feedback, command framing, UART backpressure and rollover passed\n";
  testConfirmedWiringAndAxisDirections();
  std::cout<<"confirmed encoder wiring, independent shaft feedback and all three axis directions passed\n";
  testInsertOutQuarterTurn();
  std::cout<<"insert out quarter-turn with unchanged physical direction and negative public feedback passed\n";
  testLimitCaptureResetAndIsolation();
  std::cout<<"fresh limit capture, independent axes, reset clearing both bounds and idle guards passed\n";
  testLimitEnforcementAndRecoveryDirection();
  std::cout<<"finite/legacy/held limits, each-pulse physical boundaries, jitter and inward recovery passed\n";
  testLimitTrackingLossAndSerialContract();
  std::cout<<"lost tracking with limits, safe fault recovery and direct serial limit protocol passed\n";
  testSixAnalogSwitchesAndThresholds();
  std::cout<<"six analog switches, thresholds, startup interlocks, independent stops and inward escape passed\n";
  testSwitchBounceResetEscapeAndCorrection();
  std::cout<<"switch debounce, no bounce resume, explicit reset/clear, escape and reverse correction interlocks passed\n";
  testSwitchContactsPreserveCoordinatesAndFaults();
  std::cout<<"ordinary contacts preserve coordinates and bounds without capture-related faults passed\n";
  testSwitchLimitStreamingAndFraming();
  std::cout<<"live LIM switch snapshots, shared sequence, unchanged bounds and UART command framing passed\n";
  testDirectionalClearSerialSides();
  std::cout<<"directional v2 lower/CW and upper/CCW serial clearing with unchanged physical directions passed\n";
  testIdleContactsPreserveBoundsAndIsolateAxes();
  std::cout<<"idle hand motion, both contact sides, unchanged saved/unset bounds and axis isolation passed\n";
  testExplicitZeroAndOrdinaryContactFaultPreservation();
  std::cout<<"explicit atomic zero, repeat ordinary contacts, fault preservation and range-edge bounds passed\n";
  testContactStreamingAndAllMotionRelease();
  std::cout<<"coherent unchanged-bound LIM snapshots, finite/held/start interlocks and released motion passed\n";
  testUpperTravelPositiveFromLowerOnEveryAxis();
  std::cout<<"all three axes explicit zero to positive upper travel, unchanged DIR/raw, public diagnostics and bounds passed\n";
  testHeldRampReferenceAndRepeatedPresses();
  std::cout<<"held ramp reference integration, bounded output acceleration, all axes/directions and repeated presses passed\n";
  testHeldRampAccelerationSensitivePlantAndLegacyStart();
  std::cout<<"smooth held startup prevents acceleration-sensitive step loss while legacy start remains immediate passed\n";
  testHeldRampCorrectionHeadroomAndImmediateStops();
  std::cout<<"held correction headroom, smooth recovery, immediate stops/watchdog and near-boundary startup passed\n";
  testAutonomousCalibrationAllAxesAndSavedSettings();
  std::cout<<"autonomous calibration all axes/directions, parallel phases, selection isolation and restored settings passed\n";
  testNormalContactsAfterCalibrationPreserveOriginAndBounds();
  std::cout<<"post-calibration hand motion and contacts preserve origin/range, inward escape and release commands passed\n";
  testCalibrationPressedSwitchesOddSpanAndBusyDebounce();
  std::cout<<"already-pressed switch calibration, explicit reset compatibility, odd midpoint and transient busy guards passed\n";
  testCalibrationCancellationTimeoutAndFaults();
  std::cout<<"calibration phase cancellation, bounce wait, missing-switch timeout, feedback faults and invalid span passed\n";
  testAbsolutePositionsAllAxesAndDirections();
  std::cout<<"calibrated absolute positions all axes/directions, endpoints, rounding, no-op and restored direction passed\n";
  testAbsoluteEligibilityAndSignedSerialParsing();
  std::cout<<"absolute calibration gates, manual invalidation, held endpoint, signed serial parsing and fresh feedback passed\n";
  testAbsoluteCancellationFaultsAndSelectionIsolation();
  std::cout<<"absolute cancellation/switch/stall recovery, retained calibration and concurrent selection isolation passed\n";
  testHeldDirectionPauseRecoveryAndSlowSpeed();
  std::cout<<"held direction pause/retry all axes/speeds, frozen pulses/reference and preserved bounds/zero passed\n";
  testHeldDirectionHardBudgetFrequencyAndInstability();
  std::cout<<"held significant/cumulative reverse, sliding-frequency, instability timeout and genuine wrong-direction faults passed\n";
  testHeldDirectionCancellationAndStrictOtherModes();
  std::cout<<"held warning cancellation on stop/switch/watchdog/encoder fault and strict non-held guards passed\n";
  testHeldDirectionHistoryAndCalibrationReset();
  std::cout<<"warning history persistence, greater-than-five advisory threshold, calibration reset, saturation and rollover passed\n";
  testHeldWarningTelemetryAndSharedFraming();
  std::cout<<"held warning status/LIM pause-resume history, command coexistence and fixed-buffer framing passed\n";
  testBoundedSerialBuffersAndRecovery();
  std::cout<<"maximum-width TEL/LIM frames, full serial line acceptance, atomic overflow discard and recovery passed\n";
}
