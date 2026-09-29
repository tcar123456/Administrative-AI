"""Operator recovery commands; secret values are entered interactively, never via argv."""
import argparse
from getpass import getpass
from sqlalchemy import select, delete
from app.config import settings
from app.db import SessionLocal, Account, LoginSession, AdminEvent
from app.security import setup_secret, passwords


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command', choices=['setup-code','reset-password','new-key'])
    parser.add_argument('--username')
    args=parser.parse_args()
    if args.command=='new-key':
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
    elif args.command=='setup-code':
        code=setup_secret(settings)
        if not code:
            raise SystemExit('請由部署者設定 SETUP_TOKEN。')
        print(code)
    else:
        if not args.username:
            raise SystemExit('請提供 --username 帳號。')
        first=getpass('New password (12-128 characters): ')
        second=getpass('Confirm password: ')
        if first!=second or not 12<=len(first)<=128:
            raise SystemExit('密碼不一致或長度不符。')
        with SessionLocal.begin() as db:
            user=db.scalar(select(Account).where(Account.username==args.username.lower()))
            if not user: raise SystemExit('找不到帳號。')
            user.password_hash=passwords.hash(first)
            db.execute(delete(LoginSession).where(LoginSession.account_id==user.id))
            db.add(AdminEvent(actor='server-operator',action='password_reset',target=user.id))
        print('密碼已更新，既有登入已失效。')


if __name__=='__main__':
    main()
