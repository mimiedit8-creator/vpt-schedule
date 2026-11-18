from pathlib import Path
import runpy

script = Path(__file__).with_name("vpt_bot_gemini_togglee.py")
runpy.run_path(str(script), run_name="__main__")
