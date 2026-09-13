from .database import Base, engine
from . import models  # noqa: F401


def initialise_database() -> None:
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    initialise_database()
    print("Database tables created successfully")