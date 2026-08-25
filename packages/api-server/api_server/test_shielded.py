"""F-138 regression: the class defect is 'DB work aborted mid-transaction
by outer cancellation'. shielded() is what rmf_gateway routes every
process_msg through; these tests pin its contract."""
import asyncio
import unittest

from api_server.shielded import shielded


class ShieldedTest(unittest.IsolatedAsyncioTestCase):
    async def test_inner_work_finishes_when_the_caller_is_cancelled(self):
        finished = []

        async def db_op():
            await asyncio.sleep(0.05)
            finished.append(True)

        async def handler():
            await shielded(db_op())

        task = asyncio.ensure_future(handler())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(finished, [True],
                         "cancellation aborted the shielded operation — "
                         "this is exactly the F-138 leak")

    async def test_cancellation_still_propagates_to_the_caller(self):
        async def db_op():
            await asyncio.sleep(0.05)

        task = asyncio.ensure_future(shielded(db_op()))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_plain_completion_returns_the_result(self):
        async def db_op():
            return 42

        self.assertEqual(await shielded(db_op()), 42)

    async def test_inner_exception_propagates(self):
        async def db_op():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await shielded(db_op())


if __name__ == "__main__":
    unittest.main()
