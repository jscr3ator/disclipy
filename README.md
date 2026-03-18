# disclipy

share your precious clips to discord faster than ever,
inspo from @vivaancode, made one for obs
## Features

* automatically watches your folder of choice for files
* Auto upload or manual **(hotkey avail in settings :D)**
* Supports multiple upload services (fallback if one fails), **[Gofile, Catbox, Litterbox, Buzzheavier]**
* Sends file info + link to Discord webhook, configured in settings gui
* Optional overlay showing status **[kinda buns ill fix later dw haha]**
* Tray icon for quick viewing


## Install EXE
click [here](https://github.com/jscr3ator/disclipy/releases/tag/early_release) or check releases =P

## Install through source - not reccomended 

1. Clone the repo

```
git clone https://github.com/jscr3ator/disclipy.git
cd disclipy
```

2. Install dependencies

```
pip install requests pillow imageio watchdog pystray pynput
```

## Run

```
python main.py
```

## Setup

* Set a folder to watch
* Paste your Discord webhook URL
* Choose mode:

  * Auto upload
  * Manual (uses keybind, default F8)

Settings are saved in `settings.json`.

## Screenshots

| Interface                                                                                                  | Discord Output                                                                                             |
| ---------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| <img src="https://github.com/user-attachments/assets/6dc8dcae-13a8-4bc5-b061-87ba9d56f59b" width="100%" /> | <img src="https://github.com/user-attachments/assets/b8e388c7-4a09-4c66-aca6-0d0cddfd820d" width="100%" /> |
