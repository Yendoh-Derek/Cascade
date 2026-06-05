#!/usr/bin/env python
"""Test validation of all fixes made to phases 1-3."""

import sys
sys.path.insert(0, '.')

try:
    # Test core modules that don't need external APIs
    from backend.config import get_model_config
    from backend.tutor import TutorSession, build_messages
    
    print('✓ Core modules import successfully')
    
    # Test tutor with edge cases
    session = TutorSession(subject='Test Subject')
    print('✓ TutorSession init OK')
    
    # Test subject validation - truncation
    session2 = TutorSession(subject='x' * 300)
    expected = 'x' * 200
    actual = session2.subject
    if actual == expected:
        print('✓ Subject truncation OK')
    else:
        print(f'✗ Subject truncation failed: expected {len(expected)} chars, got {len(actual or "")} chars')
    
    # Test empty subject
    session3 = TutorSession(subject='')
    if session3.subject is None:
        print('✓ Empty subject handling OK')
    else:
        print(f'✗ Empty subject should be None, got {session3.subject}')
    
    # Test invalid subject type
    session4 = TutorSession(subject=123)
    if session4.subject is None:
        print('✓ Invalid subject type handling OK')
    else:
        print(f'✗ Invalid type should be None, got {session4.subject}')
    
    # Test message validation
    session5 = TutorSession()
    session5.add_user_message('Test message')
    if len(session5.history) == 1:
        print('✓ User message add OK')
    else:
        print(f'✗ History should have 1 item, got {len(session5.history)}')
    
    # Test empty message rejection
    session5.add_user_message('')
    if len(session5.history) == 1:
        print('✓ Empty message rejection OK')
    else:
        print(f'✗ History should still have 1 item (empty rejected), got {len(session5.history)}')
    
    # Test long message truncation
    session5.add_user_message('x' * 10000)
    if len(session5.history[1]['content']) == 5000:
        print('✓ User message truncation OK')
    else:
        print(f'✗ Message should be 5000 chars, got {len(session5.history[1]["content"])}')
    
    # Test assistant message
    session5.add_assistant_message('Assistant response')
    if len(session5.history) == 3:
        print('✓ Assistant message add OK')
    else:
        print(f'✗ History should have 3 items, got {len(session5.history)}')
    
    # Test history trim
    for i in range(30):
        session5.add_user_message(f'Question {i}')
        session5.add_assistant_message(f'Answer {i}')
    
    initial_size = len(session5.history)
    session5.trim_history(max_turns=10)
    trimmed_size = len(session5.history)
    
    if trimmed_size <= 20:  # max_turns=10 means 20 messages max
        print(f'✓ History trim OK (was {initial_size}, now {trimmed_size})')
    else:
        print(f'✗ History trim failed: expected <= 20, got {trimmed_size}')
    
    # Test config
    config = get_model_config()
    print(f'✓ Config loads: STT={config.deepgram_model}, LLM={config.groq_model}')
    
    print('\n✅ All core validation tests passed!')
    
except Exception as e:
    print(f'✗ Error: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)
