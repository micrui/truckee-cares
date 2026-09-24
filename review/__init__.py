"""Local review tool for Truckee Community Cares applications.

Runs on a board member's Mac. Pulls encrypted applications from the Worker,
decrypts them with the season key, keeps a family database, matches
applications to families, asks Claude to judge ambiguous pairs, and queues
tasks for a human. Nothing here ever runs on a server.
"""
