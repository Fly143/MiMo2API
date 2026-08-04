"""身份认证模块"""

import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from .config import config_manager

security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """验证管理员凭据"""
    current_username = credentials.username.encode("utf8")
    current_password = credentials.password.encode("utf8")

    correct_username = b"admin"
    correct_password = config_manager.config.admin_password.encode("utf8")

    is_correct_username = secrets.compare_digest(current_username, correct_username)
    is_correct_password = secrets.compare_digest(current_password, correct_password)

    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
