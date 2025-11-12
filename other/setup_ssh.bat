@echo off
REM Check if the SSH key already exists
if not exist "%USERPROFILE%\.ssh\id_ed25519" (
    ssh-keygen -t ed25519 -b 4096 -f "%USERPROFILE%\.ssh\id_ed25519" -N ""
)

REM Set permissions for the private key
set PRIVATEKEYPATH=%USERPROFILE%\.ssh\id_ed25519
icacls "%PRIVATEKEYPATH%" /inheritance:r /grant "%USERNAME%:F" /remove "Users"

REM Set variables for Raspberry Pi connection
set USER_AT_HOST=pi@car2.local
set PUBKEYPATH=%USERPROFILE%\.ssh\id_ed25519.pub

REM Copy the public key to the Raspberry Pi
ssh-copy-id -i "%PUBKEYPATH%" "%USER_AT_HOST%"

REM Add the public key to the Raspberry Pi's authorized_keys manually
set PUBKEY=
for /f "usebackq tokens=*" %%i in ("%PUBKEYPATH%") do set PUBKEY=%%i
ssh "%USER_AT_HOST%" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo %PUBKEY% >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

echo SSH setup complete.
pause