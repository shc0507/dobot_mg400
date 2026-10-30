#ifndef MOTOR_CONTROL_H
#define MOTOR_CONTROL_H

#include <Arduino.h>

// Movement commands act on the selected motor; microstepping is global.
// Selecting another motor does not
// stop an existing move; update() services all three motors independently.
namespace MotorControl {

enum class Motor : uint8_t { Bottom_Motor, Top_Motor, Insert_Motor };
enum class Direction : uint8_t { Clockwise, Counterclockwise };
enum class HomeError : uint8_t {
  None, Busy, NoMinimum, Timeout, StepLimit, Maximum, BothLimits, NotReleased,
  PositionRange, Cancelled
};

struct Status {
  Direction direction;
  bool rotating;
  bool continuous;
  long position;        // Signed STEP pulses since begin()/home(), not measured travel.
  long remainingSteps; // Signed; zero in continuous mode.
  float speed;         // Configured speed limit, STEP pulses/second.
  float acceleration;  // STEP pulses/second squared, for finite moves.
  uint16_t stepsPerRevolution;
  bool homed;
  bool limitMin;
  bool limitMax;
  bool soft;
  bool awake;
};

// Wiring is configured in MotorPins.h; re-upload the sketch after editing it.
// Defaults: bottom selected, CW, 200 steps/rev, 200 steps/s, 50 steps/s^2.
// All drivers start asleep. Accepted motion wakes only that driver, with a
// non-blocking 2 ms wait before stepping. Each sleeps after SleepTimeoutMs idle
// (editable in MotorPins.h). keepAwake() can extend this interval without motion.
// Selection, status, and other settings do not wake drivers.
void begin();
void update(); // Call frequently from loop(); do not add blocking delays.
bool selectMotor(Motor motor);
Motor selectedMotor();

// Shared MS1/MS2/MS3: all motors must be idle. Startup mode is full step (1).
bool setMicrostep(uint8_t divisor); // 1, 2, 4, 8, or 16; no movement or counter reset.
uint8_t microstep();

// Configuration requires the selected motor to be idle. CW means DIR HIGH;
// physical clockwise/counterclockwise depends on coil wiring and viewpoint.
bool setDirection(Direction direction);
bool setSpeed(float stepsPerSecond);                // 1..1000 for this demo.
bool setAcceleration(float stepsPerSecondSquared); // 1..10000.
bool setStepsPerRevolution(uint16_t steps);         // Full steps, e.g. 200 or 400.

// Positive magnitudes use the selected direction. Finite moves accelerate
// and decelerate. Calls return immediately; update() performs the movement.
// False means not initialized, invalid input/range, or selected motor busy.
bool rotateSteps(long steps); // STEP pulses at the current microstep resolution.
bool rotateRevolutions(float revolutions); // Rounded to the nearest STEP pulse.
bool rotateOneRevolution();

// Continuous rotation starts at the configured constant speed, without a ramp.
bool startRotation();
// GUI hold: stop automatically unless renewed at least once per second.
bool startHold();
bool keepHoldAlive();
void stopRotation(); // Immediately cancel selected motor's pulses/target.
void stopAll();      // Immediately cancel every motor's pulses/target.

// Nonblocking, bounded homing in CCW (negative) direction. The final minimum
// contact establishes zero. Stop cancels homing and never establishes a reference.
HomeError home();
HomeError takeHomeError(Motor motor); // Consume a deferred failure, outside printing.
const __FlashStringHelper *homeErrorText(HomeError error);
bool setSoftLimits(long minimum, long maximum); // Idle and homed only, inclusive.
bool keepAwake(unsigned long milliseconds); // 0..60000, renewable; 0 cancels extension.

// Stopping holds coils until that driver's idle sleep timeout, then SLEEP goes
// LOW and releases them. No A4988 EN pin is controlled here.
bool isRotating(Motor motor);
bool getStatus(Motor motor, Status &status);

} // namespace MotorControl

#endif
