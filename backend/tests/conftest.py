import os, sys, tempfile, pathlib
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + str(pathlib.Path(tempfile.mkdtemp()) / 'test.db'))
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/15')
os.environ.setdefault('ARTIFACT_DIR', tempfile.mkdtemp())
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('SEED_DEMO', 'false')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
