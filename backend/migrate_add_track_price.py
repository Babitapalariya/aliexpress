# migrate_add_track_price.py
from sqlalchemy import text, inspect
from app.database import engine

def add_track_price_column():
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns('imported_products')]
    
    if 'track_price' not in columns:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE imported_products ADD COLUMN track_price BOOLEAN DEFAULT TRUE NOT NULL"
            ))
            conn.commit()
        print("✅ Column 'track_price' added successfully.")
    else:
        print("ℹ️ Column 'track_price' already exists.")

if __name__ == "__main__":
    add_track_price_column()