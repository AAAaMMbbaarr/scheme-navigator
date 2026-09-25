"""Dev launcher: real app, but Claude replaced by a MOCK so the multilingual UI path can be driven."""
import sys, uvicorn
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from tests.conftest import FakeClaude
PROFILES = {
 "I am a student from Bihar.": {"state": "Bihar", "occupation": "student"},
 "I am a farmer from West Bengal.": {"state": "West Bengal", "occupation": "farmer"},
 "I am a farmer from West Bengal with 2 acres of land.": {"state": "West Bengal", "occupation": "farmer", "land_area_acres": 2, "land_ownership": "owner"},
 "আমি পশ্চিমবঙ্গের একজন কৃষক। আমার ২ একর জমি আছে।": {"state": "West Bengal", "occupation": "farmer", "land_area_acres": "২", "land_ownership": "owner"},
 "मैं बिहार का किसान हूँ और मेरे पास 2 एकड़ जमीन है।": {"state": "Bihar", "occupation": "farmer", "land_area_acres": 2, "land_ownership": "owner"},
 "I own 3 acres": {"land_area_acres": 3},
}
fake = FakeClaude(profiles=PROFILES)
import backend.profile_extractor as pe, backend.explainer as ex
pe.get_client = lambda: fake; ex.get_client = lambda: fake
from backend.main import app
uvicorn.run(app, port=8766, log_level="warning")
