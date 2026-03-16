# JARVIS Testing Skill
After making ANY code change to JARVIS:
1. Run: jrun "open calculator" — tests basic app launching
2. Run: jrun "go to github.com" — tests browser
3. Run: jrun "create a folder called test123 on my desktop" — tests file ops
4. If all 3 pass, the change is good
5. If any fail, revert and try again
NEVER tell the user a fix is done without running these 3 tests first.
