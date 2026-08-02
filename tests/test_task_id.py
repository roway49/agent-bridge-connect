import unittest


class TaskIdAllocationTests(unittest.TestCase):
    def test_default_four_character_capacity(self):
        from agent_bridge_connect.task_id import (
            CHAIN_TOKEN_ALPHABET,
            DEFAULT_CHAIN_TOKEN_LENGTH,
            token_capacity,
        )

        self.assertEqual(DEFAULT_CHAIN_TOKEN_LENGTH, 4)
        self.assertEqual(len(CHAIN_TOKEN_ALPHABET), 30)
        self.assertEqual(token_capacity(DEFAULT_CHAIN_TOKEN_LENGTH), 810000)

    def test_token_length_increases_only_when_capacity_is_exhausted(self):
        from agent_bridge_connect.task_id import (
            CHAIN_TOKEN_ALPHABET,
            token_length_for_available_capacity,
        )

        self.assertEqual(token_length_for_available_capacity([], min_length=1), 1)
        self.assertEqual(
            token_length_for_available_capacity(CHAIN_TOKEN_ALPHABET, min_length=1),
            2,
        )

    def test_deleted_task_token_returns_capacity(self):
        from agent_bridge_connect.task_id import (
            CHAIN_TOKEN_ALPHABET,
            token_length_for_available_capacity,
        )

        tokens = set(CHAIN_TOKEN_ALPHABET)
        tokens.remove(next(iter(tokens)))

        self.assertEqual(token_length_for_available_capacity(tokens, min_length=1), 1)

    def test_allocate_uses_hash_membership_and_returns_unused_token(self):
        from agent_bridge_connect.task_id import CHAIN_TOKEN_ALPHABET, allocate_chain_token

        token = allocate_chain_token(CHAIN_TOKEN_ALPHABET, min_length=1)

        self.assertEqual(len(token), 2)
        self.assertNotIn(token, set(CHAIN_TOKEN_ALPHABET))


if __name__ == "__main__":
    unittest.main()
