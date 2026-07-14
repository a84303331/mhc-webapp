"""List users and set daily_limit to 0 for test accounts"""
import asyncio
import sys
from database import async_session
from models import User
from sqlalchemy import select, update

async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None

    async with async_session() as db:
        result = await db.execute(select(User).order_by(User.id))
        users = result.scalars().all()

        for u in users:
            print(f"ID={u.id} name={u.name} email={u.email} limit={u.daily_limit} admin={u.is_admin}")

        if target:
            for u in users:
                if target.lower() in u.name.lower() or target.lower() in u.email.lower():
                    await db.execute(
                        update(User).where(User.id == u.id).values(daily_limit=0)
                    )
                    await db.commit()
                    print(f"\n✅ {u.name} ({u.email}) → daily_limit=0 (無限)")
                    break
            else:
                print(f"\n❌ 找不到 '{target}' 使用者")

asyncio.run(main())
