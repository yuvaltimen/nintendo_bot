import time

# Move forward using Left Stick Up
pad.set_axis(Sticks.LEFT_STICK, 0, 1)  # Assuming (x, y) where y is for up/down
time.sleep(0.5)

# Jump with X button
pad.press(Buttons.X)
time.sleep(0.2)

# Release jump to land
pad.release(Buttons.X)
time.sleep(1)

# Move backward using Left Stick Down
pad.set_axis(Sticks.LEFT_STICK, 0, -1)  # (x, y) where y is for up/down
time.sleep(0.5)

# Attack with Y button
pad.press(Buttons.Y)
time.sleep(0.2)

# Release attack
pad.release(Buttons.Y)
time.sleep(1)

# Strafe left using Left Stick Left
pad.set_axis(Sticks.LEFT_STICK, -1, 0)  # (x, y) where x is for left/right
time.sleep(0.5)

# Strafe right using Left Stick Right
pad.set_axis(Sticks.LEFT_STICK, 1, 0)
time.sleep(0.5)

# Stop strafing to neutral position
pad.set_axis(Sticks.LEFT_STICK, 0, 0)
time.sleep(1)

# Turn around by moving left and then right with Left Stick
pad.set_axis(Sticks.LEFT_STICK, -1, 0)  # Strafe/turn left
time.sleep(0.5)
pad.set_axis(Sticks.LEFT_STICK, 1, 0)   # Strafe/turn right
time.sleep(0.5)

# Release all keys to neutralize control
pad.release_all()