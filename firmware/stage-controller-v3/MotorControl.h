#ifndef MOTOR_CONTROL_H
#define MOTOR_CONTROL_H

#include <Arduino.h>
#include "Encoders.h"

// Movement commands act on the selected motor; all motors use fixed eighth stepping.
// Selecting another motor does not
// stop an existing move; update() services all three motors independently.
namespace MotorControl {

enum class Motor : uint8_t { Bottom_Motor, Top_Motor, Insert_Motor };
enum class Direction : uint8_t { Clockwise, Counterclockwise };
enum class Fault : uint8_t { None, Encoder, Stall, Direction, Tracking, Timeout };
enum class Calibration : uint8_t { None, Lower, Upper, Center, Done, Cancelled, Failed };

struct Status {
  Direction direction;
  bool rotating;
  bool continuous;
  float position;      // Public shaft rotations: positive toward upper/CCW.
  float remaining;     // Target minus measured rotations; zero in continuous mode.
  float speed;         // Configured rotations/second.
  float acceleration;  // Rotations/second squared: finite moves and held speed/correction ramps.
  uint16_t stepsPerRevolution;
  Encoders::Health encoder;
  Fault fault;
  float tolerance;
  float maxSpeed;
  float measuredSpeed;
  long positionCounts;  // Exact encoder counts for unsolicited telemetry.
  long remainingCounts;
  long lowerCounts; // Public lower endpoint: CW, the mathematical minimum.
  long upperCounts; // Public upper endpoint: CCW, the mathematical maximum.
  uint8_t bounds; // Bit 0: lower explicitly set; bit 1: upper explicitly set.
  uint8_t switchMask; // Candidate OR debounced state: bit 0 lower/CW, bit 1 upper/CCW.
  uint16_t switchRaw; // Latest paired-switch analog sample, 0..1023.
  Calibration calibration;
  bool directionPaused;
  uint16_t directionWarnings;
};

// Wiring is configured in MotorPins.h; re-upload the sketch after editing it.
// Defaults: bottom selected, CW, 200 steps/rev, 0.25 rev/s, 0.25 rev/s^2.
// All drivers start asleep. Accepted motion wakes only that driver, with a
// non-blocking 2 ms wait before stepping. Each sleeps after SleepTimeoutMs idle
// (editable in MotorPins.h). Selection, status, and settings do not wake drivers.
void begin();
void update(); // Call frequently from loop(); do not add blocking delays.
bool selectMotor(Motor motor);
Motor selectedMotor();

// Shared MS1/MS2/MS3 are HIGH/HIGH/LOW from startup (eighth stepping).
bool setMicrostep(uint8_t divisor); // Accepts only 8 while all motors idle; no change.
uint8_t microstep();

// Configuration requires the selected motor to be idle. CW means DIR HIGH;
// physical clockwise/counterclockwise depends on coil wiring and viewpoint.
bool setDirection(Direction direction);
bool setSpeed(float rotationsPerSecond);               // 0.001..maxSpeed.
bool setAcceleration(float rotationsPerSecondSquared); // 0.001..50.
bool setStepsPerRevolution(uint16_t steps);         // Full steps, e.g. 200 or 400.

// Positive magnitudes use the selected direction. Finite moves accelerate
// and decelerate. Calls return immediately; update() performs the movement.
// False means not initialized, invalid input/range, or selected motor busy.
bool rotateSteps(long steps); // Legacy input converted into a closed-loop rotation target.
bool rotateRevolutions(float revolutions); // 0.001..10000, nearest encoder count.
bool rotateOneRevolution();
bool goToPosition(float rotations); // Public absolute target, within a successfully calibrated range.

// Continuous rotation starts at the configured constant speed, without a ramp.
bool startRotation();
// GUI hold: ramp from zero using configured acceleration; stop unless renewed every second.
bool startHold();
bool keepHoldAlive();
bool calibrate(); // Autonomous lower switch -> upper switch -> measured midpoint; idle only.
void stopRotation(); // Immediately cancel selected motor's pulses/target.
void stopAll();      // Immediately cancel every motor's pulses/target.

// Stopping holds coils until that driver's idle sleep timeout, then SLEEP goes
// LOW and releases them. No A4988 EN pin is controlled here.
bool isRotating(Motor motor);
bool getStatus(Motor motor, Status &status);
bool clearFault(); // Selected idle motor; healthy encoder required; never starts motion.
bool resetPosition(); // Fresh healthy/trusted idle axis: zero position, clear BOTH limits.
bool setLimit(); // Public positive position sets upper/CCW, negative sets lower/CW; zero rejected.
bool clearUpperLimit(); // Idle, any health; restore positive/CCW public default, no movement.
bool clearLowerLimit(); // Idle, any health; restore negative/CW public default, no movement.
const __FlashStringHelper *faultName(Fault fault);
const __FlashStringHelper *calibrationName(Calibration calibration);
const __FlashStringHelper *warningName(bool paused);

} // namespace MotorControl

#endif
