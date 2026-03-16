# Browser Automation Skill
When asked to interact with any website:
1. Always use browser_open first to navigate
2. Wait 2 seconds for page load
3. Use browser_search for searching within sites
4. Use browser_click with CSS selectors for clicking elements
5. Always take a screenshot to verify result
Common selectors:
- YouTube search: input[name="search_query"]
- Google search: input[name="q"]  
- GitHub search: input[placeholder*="Search"]

CRITICAL: For YouTube search ALWAYS use browser_search tool with engine='youtube'. NEVER use browser_type or browser_click for YouTube. The browser_search tool handles YouTube search internally.
