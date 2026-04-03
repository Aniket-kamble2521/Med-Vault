from flask_login import UserMixin


class User(UserMixin):
    def __init__(self, user_id: int, username: str) -> None:
        self.id = str(user_id)
        self.username = username

