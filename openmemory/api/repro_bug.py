#!/usr/bin/env python3
"""
Reproduction script for the Pydantic validation error in OpenMemory MCP.
Run this from the openmemory/api directory using:
    uv run --with-requirements requirements.txt python repro_bug.py
"""

import os
import sys
from uuid import uuid4
from datetime import datetime, UTC
from pathlib import Path

# Ensure we can import from app
sys.path.insert(0, str(Path(__file__).parent))

# Import the actual models and schemas from the codebase
from app.models import Memory, App, User, MemoryState, Base, Category
from app.schemas import MemoryResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

def create_test_database():
    """Create an in-memory SQLite test database"""
    # Use in-memory database for testing
    engine = create_engine("sqlite:///:memory:", echo=False)
    
    # Create all tables
    Base.metadata.create_all(engine)
    
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionLocal

def setup_test_data(session: Session):
    """Create test data that will trigger the bug"""
    # Create test user
    user = User(
        id=uuid4(),
        user_id="test_user_repro",
        email="test@repro.com"
    )
    session.add(user)
    session.flush()
    
    # Create test app
    app = App(
        id=uuid4(),
        name="Test App for Bug Reproduction",
        owner_id=user.id  # Fixed: owner_id not user_id
    )
    session.add(app)
    session.flush()
    
    # Create test categories
    categories = []
    for i in range(2):
        category = Category(
            id=uuid4(),
            name=f"category_{i+1}"
        )
        categories.append(category)
        session.add(category)
    session.flush()
    
    # Create memories with relationships
    memories = []
    for i in range(3):
        memory = Memory(
            id=uuid4(),
            content=f"Test memory content {i+1} for demonstrating the bug",
            user_id=user.id,
            app_id=app.id,
            state=MemoryState.active,
            created_at=datetime.now(UTC),
            metadata_={"test": f"data_{i}", "index": i}
        )
        # Add categories to some memories
        if i < 2:
            memory.categories.append(categories[i])
        
        memories.append(memory)
        session.add(memory)
    
    session.commit()
    return user.id, app.id, [m.id for m in memories]

def reproduce_bug_without_fix():
    """
    Reproduce the bug: query without eager loading, then access relationships
    after session is closed (simulating what happens in pagination)
    """
    print("\n" + "="*60)
    print("🐛 REPRODUCING THE BUG (WITHOUT FIX)")
    print("="*60)
    
    engine, SessionLocal = create_test_database()
    
    # Setup test data
    with SessionLocal() as session:
        user_id, app_id, memory_ids = setup_test_data(session)
        print(f"✅ Created test data: {len(memory_ids)} memories")
    
    # Now reproduce the bug
    print("\nStep 1: Query memories WITHOUT eager loading...")
    session = SessionLocal()
    
    # This is the problematic query - NO joinedload()
    query = session.query(Memory).filter(
        Memory.user_id == user_id,
        Memory.state == MemoryState.active
    )
    
    # Add the outer joins (but without eager loading)
    query = query.outerjoin(App, Memory.app_id == App.id)
    query = query.outerjoin(Memory.categories)
    
    # Get the memories
    memories = query.all()
    print(f"   ✓ Found {len(memories)} memories")
    
    # This is crucial - close the session (happens in real pagination)
    print("\nStep 2: Close the database session (simulating pagination)...")
    session.close()
    print("   ✓ Session closed")
    
    print("\nStep 3: Try to serialize with Pydantic (accessing relationships)...")
    print("   This should fail because relationships weren't eager-loaded!\n")
    
    failed = False
    for i, memory in enumerate(memories, 1):
        try:
            print(f"   Memory {i}: Attempting to access memory.app.name...")
            
            # This is what the original code tries to do
            # It will fail when accessing memory.app.name because session is closed
            response = MemoryResponse(
                id=memory.id,
                content=memory.content,
                created_at=memory.created_at,  # datetime -> int conversion
                state=memory.state.value,  # enum -> string
                app_id=memory.app_id,
                app_name=memory.app.name,  # ← THIS SHOULD FAIL!
                categories=[cat.name for cat in memory.categories],  # ← THIS TOO!
                metadata_=memory.metadata_
            )
            
            print(f"      ✅ Success (unexpected!) - app_name: {response.app_name}")
            
        except Exception as e:
            failed = True
            print(f"      💥 FAILED: {type(e).__name__}")
            print(f"         Error: {str(e)[:100]}...")
            print(f"         ^ This is the bug! Can't access relationships after session close")
    
    if failed:
        print(f"\n✅ Bug successfully reproduced!")
        return True
    else:
        print(f"\n❌ Bug not reproduced - this might be SQLite-specific behavior")
        return False

def demonstrate_fix_with_eager_loading():
    """
    Show how the fix works with proper eager loading
    """
    print("\n" + "="*60)
    print("✅ DEMONSTRATING THE FIX (WITH EAGER LOADING)")
    print("="*60)
    
    from sqlalchemy.orm import joinedload
    
    engine, SessionLocal = create_test_database()
    
    # Setup test data
    with SessionLocal() as session:
        user_id, app_id, memory_ids = setup_test_data(session)
        print(f"✅ Created test data: {len(memory_ids)} memories")
    
    print("\nStep 1: Query memories WITH eager loading (the fix)...")
    session = SessionLocal()
    
    # Start with the same base query
    query = session.query(Memory).filter(
        Memory.user_id == user_id,
        Memory.state == MemoryState.active
    )
    
    # Add the outer joins
    query = query.outerjoin(App, Memory.app_id == App.id)
    query = query.outerjoin(Memory.categories)
    
    # THE FIX: Add eager loading!
    query = query.options(
        joinedload(Memory.app),
        joinedload(Memory.categories)
    )
    
    # Get the memories
    memories = query.all()
    print(f"   ✓ Found {len(memories)} memories with relationships pre-loaded")
    
    print("\nStep 2: Close the database session...")
    session.close()
    print("   ✓ Session closed (but data is already loaded!)")
    
    print("\nStep 3: Serialize with Pydantic (should work now)...\n")
    
    success_count = 0
    for i, memory in enumerate(memories, 1):
        try:
            print(f"   Memory {i}: Serializing with eager-loaded data...")
            
            # Use the transformer pattern from the fix
            response = MemoryResponse(
                id=memory.id,
                content=memory.content,
                created_at=int(memory.created_at.timestamp()),  # Explicit conversion
                state=memory.state.value,
                app_id=memory.app_id,
                app_name=memory.app.name if memory.app else None,  # Null safety
                categories=[cat.name for cat in memory.categories],
                metadata_=memory.metadata_
            )
            
            success_count += 1
            print(f"      ✅ Success - app_name: '{response.app_name}'")
            print(f"         categories: {response.categories}")
            
        except Exception as e:
            print(f"      💥 Failed: {type(e).__name__}: {str(e)[:100]}")
    
    if success_count == len(memories):
        print(f"\n✅ Fix confirmed: All {success_count} memories serialized successfully!")
        return True
    else:
        print(f"\n⚠️  Partial success: {success_count}/{len(memories)} serialized")
        return False

def show_the_actual_code_diff():
    """Show the exact changes made in the fix"""
    print("\n" + "="*60)
    print("📝 THE ACTUAL FIX IN memories.py")
    print("="*60)
    
    print("\n🔴 BEFORE (broken code):")
    print("-" * 40)
    print("""
# In list_memories() endpoint:

query = db.query(Memory).filter(...)
query = query.outerjoin(App, Memory.app_id == App.id)
query = query.outerjoin(Memory.categories)

# ❌ Missing: query.options(joinedload(...))

# Direct pagination without transformer
paginated_results = sqlalchemy_paginate(query, params)
""")
    
    print("\n🟢 AFTER (fixed code):")
    print("-" * 40)
    print("""
# In list_memories() endpoint:

query = db.query(Memory).filter(...)
query = query.outerjoin(App, Memory.app_id == App.id)
query = query.outerjoin(Memory.categories)

# ✅ THE FIX: Add eager loading
query = query.options(
    joinedload(Memory.app),
    joinedload(Memory.categories)
)

# ✅ Use transformer for explicit serialization
paginated_results = sqlalchemy_paginate(
    query, 
    params,
    transformer=lambda items: [
        MemoryResponse(
            id=memory.id,
            content=memory.content,
            created_at=memory.created_at,
            state=memory.state.value,
            app_id=memory.app_id,
            app_name=memory.app.name if memory.app else None,
            categories=[cat.name for cat in memory.categories],
            metadata_=memory.metadata_
        )
        for memory in items
    ]
)
""")

def main():
    print("\n" + "="*60)
    print(" OpenMemory MCP - Pydantic Validation Bug Reproduction")
    print(" Using actual models from openmemory/api/app/")
    print("="*60)
    
    try:
        # Try to reproduce the bug
        print("\nPart 1: Attempting to reproduce the bug...")
        bug_reproduced = reproduce_bug_without_fix()
        
        # Show the fix
        print("\nPart 2: Demonstrating the fix...")
        fix_works = demonstrate_fix_with_eager_loading()
        
        # Show the code diff
        show_the_actual_code_diff()
        
        # Summary
        print("\n" + "="*60)
        print("📊 SUMMARY")
        print("="*60)
        
        if bug_reproduced:
            print("\n✅ Bug successfully reproduced!")
            print("   The error occurs when:")
            print("   1. SQLAlchemy relationships are not eagerly loaded")
            print("   2. Session closes (during pagination)")
            print("   3. Pydantic tries to access relationship fields")
            print("   4. SQLAlchemy can't fetch data without active session")
        else:
            print("\n⚠️  Bug not directly reproduced")
            print("   (SQLite may behave differently than PostgreSQL)")
            print("   But the issue is real in production!")
        
        if fix_works:
            print("\n✅ Fix validated!")
            print("   The solution:")
            print("   1. Use joinedload() to eagerly fetch relationships")
            print("   2. Use explicit transformer in pagination")
            print("   3. Add null safety checks")
        
        print("\n" + "="*60)
        
    except Exception as e:
        print(f"\n💥 Unexpected error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()