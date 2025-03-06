import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from pathlib import Path
import datetime
import asyncio
import sqlite3
import random
import pytz
import os

def init_database():
    # Create the database in the same directory as the script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(script_dir, "server_settings.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS channel_settings
                 (server_id TEXT, channel_id TEXT, max_messages INTEGER, keep_pinned BOOLEAN,
                  PRIMARY KEY (server_id, channel_id))''')
    # Add new table for tracking thanks
    c.execute('''CREATE TABLE IF NOT EXISTS user_thanks
                 (user_id TEXT, last_thanks_date TEXT, streak INTEGER,
                  PRIMARY KEY (user_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS server_settings
                 (guild_id TEXT, setting_name TEXT, setting_value TEXT,
                  PRIMARY KEY (guild_id, setting_name))''')
    # New table for user timezone settings
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings                       
                 (user_id TEXT, timezone TEXT DEFAULT 'UTC',
                  PRIMARY KEY (user_id))''')
    conn.commit()
    conn.close()
    return db_path

# Initialize DB_PATH after the function is defined
DB_PATH = init_database()

# Update bot setup to use slash commands
load_dotenv()
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.dm_messages = False
bot = commands.Bot(command_prefix="!", intents=intents)

def get_channel_settings(server_id: str, channel_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT max_messages, keep_pinned FROM channel_settings 
                 WHERE server_id = ? AND channel_id = ?''', (server_id, channel_id))
    result = c.fetchone()
    conn.close()
    return result if result else None

def save_channel_settings(server_id: str, channel_id: str, max_messages: int, keep_pinned: bool):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO channel_settings (server_id, channel_id, max_messages, keep_pinned)
                 VALUES (?, ?, ?, ?)''', (server_id, channel_id, max_messages, keep_pinned))
    conn.commit()
    conn.close()

def remove_channel_settings(server_id: str, channel_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''DELETE FROM channel_settings WHERE server_id = ? AND channel_id = ?''',
              (server_id, channel_id))
    conn.commit()
    conn.close()

def get_managed_channels(server_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT channel_id, max_messages, keep_pinned FROM channel_settings 
                 WHERE server_id = ?''', (server_id,))
    results = c.fetchall()
    conn.close()
    return results

# Add these new functions for managing thanks
def check_user_thanks(user_id: str) -> tuple[bool, int]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Get user's local time
    local_time = get_user_local_time(user_id)
    today = local_time.strftime('%Y-%m-%d')
    
    c.execute('SELECT last_thanks_date, streak FROM user_thanks WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    
    if not result:
        conn.close()
        return False, 0
    
    last_thanks_date, streak = result
    
    # Convert last_thanks_date string to datetime object in user's timezone
    last_thanks_dt = datetime.datetime.strptime(last_thanks_date, '%Y-%m-%d').date()
    local_dt = local_time.date()
    
    # Calculate days difference
    days_diff = (local_dt - last_thanks_dt).days
    
    # If more than 1 day has passed, reset streak
    if days_diff > 1:
        streak = 0
    
    already_thanked = (last_thanks_date == today)
    conn.close()
    return already_thanked, streak

def update_user_thanks(user_id: str, decrease_streak: bool = False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Get user's local time
    local_time = get_user_local_time(user_id)
    today = local_time.strftime('%Y-%m-%d')
    
    c.execute('SELECT last_thanks_date, streak FROM user_thanks WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    
    if result:
        last_thanks_date, current_streak = result
        last_thanks_dt = datetime.datetime.strptime(last_thanks_date, '%Y-%m-%d').date()
        local_dt = local_time.date()
        days_diff = (local_dt - last_thanks_dt).days
        
        if decrease_streak:
            new_streak = max(0, current_streak - 1)
            current_streak = new_streak + 1  # For display purposes
        elif days_diff == 1:  # Only increment if exactly one day has passed
            new_streak = current_streak + 1
        elif days_diff == 0:  # Same day
            new_streak = current_streak
        else:  # More than one day passed
            new_streak = 1
            current_streak = 0
    else:
        new_streak = 0 if decrease_streak else 1
        current_streak = 0
    
    c.execute('''INSERT OR REPLACE INTO user_thanks (user_id, last_thanks_date, streak)
                 VALUES (?, ?, ?)''', (user_id, today, new_streak))
    
    conn.commit()
    conn.close()
    return new_streak, current_streak

def get_user_local_time(user_id: str) -> datetime:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Get user's timezone, default to UTC if not set
    c.execute('SELECT timezone FROM user_settings WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    timezone = result[0] if result else 'UTC'
    conn.close()
    
    # Convert UTC time to user's local time
    utc_time = discord.utils.utcnow()
    local_tz = pytz.timezone(timezone)
    local_time = utc_time.astimezone(local_tz)
    return local_time

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    
    # Set the bot's status
    await bot.change_presence(activity=discord.Game(name="Crime Scene Cleaner"))
    
    try:
        synced = await bot.tree.sync()
        print(f"Successfully synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {str(e)}")

@bot.tree.command(name="configure", description="Configure the maid bot for a specific channel")
@app_commands.describe(
    channel="The channel to manage",
    max_messages="Maximum number of messages to keep in the channel",
    keep_pinned="Whether to preserve pinned messages (true/false)"
)
async def configure(interaction: discord.Interaction, channel: discord.TextChannel, max_messages: int, keep_pinned: bool):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command!", ephemeral=True)
        return
        
    save_channel_settings(str(interaction.guild_id), str(channel.id), max_messages, keep_pinned)
    
    # Send initial response
    await interaction.response.send_message(
        f"Channel {channel.mention} configured with max messages: {max_messages}, keep pinned: {keep_pinned}\nStarting initial cleanup...",
        ephemeral=True
    )
    
    # Perform initial cleanup
    messages = [msg async for msg in channel.history(limit=None)]
    if keep_pinned:
        messages = [msg for msg in messages if not msg.pinned]
    messages.sort(key=lambda x: x.created_at)

    if len(messages) > max_messages:
        to_delete = messages[:len(messages) - max_messages]
        
        for msg in to_delete:
            try:
                if (discord.utils.utcnow() - msg.created_at).days < 14:
                    await channel.delete_messages([msg])
                else:
                    await msg.delete()
                await asyncio.sleep(1)  # Basic rate limit prevention
            except discord.errors.HTTPException as e:
                if e.status == 429:  # Rate limit hit
                    await asyncio.sleep(e.retry_after)
                    try:
                        await msg.delete()
                    except Exception as e:
                        print(f"Failed to delete message after rate limit: {e}")
                else:
                    print(f"Error deleting message: {e}")
                    await asyncio.sleep(5)

@bot.tree.command(name="remove_channel", description="Remove a channel from being managed by the maid bot")
@app_commands.describe(
    channel="The channel to stop managing"
)
async def remove_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command!", ephemeral=True)
        return
        
    remove_channel_settings(str(interaction.guild_id), str(channel.id))
    await interaction.response.send_message(
        f"Channel {channel.mention} removed from management",
        ephemeral=True
    )

@bot.event
async def on_message(message):
    # Skip DMs
    if message.guild is None:
        return
        
    settings = get_channel_settings(str(message.guild.id), str(message.channel.id))
    if not settings or message.author == bot.user:
        return

    max_messages, keep_pinned = settings

    # Get messages based on settings
    messages = [msg async for msg in message.channel.history(limit=None)]
    if keep_pinned:
        messages = [msg for msg in messages if not msg.pinned]
    messages.sort(key=lambda x: x.created_at)

    if len(messages) > max_messages:
        to_delete = messages[:len(messages) - max_messages]
        
        # Separate messages into recent (< 14 days) and old messages
        recent_messages = []
        old_messages = []
        for msg in to_delete:
            try:
                if (message.created_at - msg.created_at).days < 14:
                    # Recent message - can use bulk delete
                    await message.channel.delete_messages([msg])
                else:
                    # Old message - must delete individually
                    await msg.delete()
                
                await asyncio.sleep(1)  # Basic rate limit prevention
                
            except discord.errors.HTTPException as e:
                if e.status == 429:  # Rate limit hit
                    retry_after = e.retry_after
                    print(f"Rate limited. Waiting {retry_after} seconds...")
                    await asyncio.sleep(retry_after)  # Wait the full duration
                    # Retry the same message
                    try:
                        await msg.delete()
                    except Exception as e:
                        print(f"Failed to delete message after rate limit: {e}")
                else:
                    print(f"Error deleting message: {e}")
                    await asyncio.sleep(5)

@bot.event
async def on_guild_join(guild):
    """Sends a welcome message when the bot joins a new server"""
    print(f"Joined new guild: {guild.name} (ID: {guild.id})")  # Debug log
    
    # Try to find the best channel to send the welcome message
    target_channel = None
    
    # First, try to find specific named channels in order of preference
    preferred_channels = ["welcome", "general", "main"]
    for channel_name in preferred_channels:
        for channel in guild.text_channels:
            if channel_name in channel.name.lower():
                permissions = channel.permissions_for(guild.me)
                if permissions.send_messages and permissions.view_channel:
                    target_channel = channel
                    break
        if target_channel:
            break
    
    # If no preferred channel found, try to find the first channel we can send messages in
    if not target_channel:
        for channel in guild.text_channels:
            permissions = channel.permissions_for(guild.me)
            if permissions.send_messages and permissions.view_channel:
                target_channel = channel
                break
    
    if target_channel:
        try:
            welcome_message = """
👋 **Thanks for adding ServerMaid!**

I help keep your channels clean by automatically managing message history. Here's how to use me:

**Commands** (Requires Administrator permissions):
`/configure #channel max_messages keep_pinned` - Set up channel management
  • Example: `/configure #general 100 true` (keeps 100 messages, preserves pins)
  • Example: `/configure #chat 50 false` (keeps 50 messages, doesn't preserve pins)

`/remove_channel #channel` - Stop managing a channel
`/list_managed_channels` - List all channels being managed

**Note:** Once configured, I'll automatically maintain the message limit in the specified channels!
Incase your channel has many more messages than the max limit you set, you might want to
duplicate the channel and delete the old one, as deletnig the old messages could take some time.
"""
            await target_channel.send(welcome_message)
            print(f"Sent welcome message in {target_channel.name}")  # Debug log
        except discord.Forbidden:
            print(f"Failed to send welcome message - Missing permissions in {target_channel.name}")
        except Exception as e:
            print(f"Error sending welcome message: {str(e)}")
    else:
        print(f"Could not find a suitable channel to send welcome message in {guild.name}")

@bot.tree.command(name="list_managed_channels", description="List all channels being managed by the bot")
async def list_managed_channels(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command!", ephemeral=True)
        return
        
    channels = get_managed_channels(str(interaction.guild_id))
    if not channels:
        await interaction.response.send_message("No channels are currently being managed.", ephemeral=True)
        return
        
    message = "**Managed Channels:**\n"
    for channel_id, max_messages, keep_pinned in channels:
        channel = interaction.guild.get_channel(int(channel_id))
        if channel:
            # Convert the numeric boolean to True/False string
            keep_pinned_str = "True" if keep_pinned else "False"
            message += f"• {channel.mention}: Max messages: {max_messages}, Keep pinned: {keep_pinned_str}\n"
    
    await interaction.response.send_message(message, ephemeral=True)

# Add the thanks command
@bot.tree.command(
    name="thanks",
    description="Thank the bot for its service!"
)
async def thanks(interaction: discord.Interaction):
    try:
        print("Starting thanks command...")
        await interaction.response.defer()
        
        user_id = str(interaction.user.id)
        
        # Check timezone first
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT timezone FROM user_settings WHERE user_id = ?', (user_id,))
        timezone_result = c.fetchone()
        conn.close()
        
        if not timezone_result:
            await interaction.followup.send(
                "Please set your timezone first using `/set_timezone`! This helps me track your daily thanks accurately.",
                ephemeral=True
            )
            return
        
        # Check if already thanked
        already_thanked, current_streak = check_user_thanks(user_id)
        
        if already_thanked:
            await interaction.followup.send(
                f"You've already thanked me today! Current streak: {current_streak} days",
                ephemeral=True
            )
            return
            
        # If we get here, user hasn't thanked today, proceed with updating thanks
        responses = [
            "Noo thank you!!",
            "You are too kind ^^",
            "Of course!",
            ":))))))))))))))",
            "Happy to help!",
            "I love you.",
            "I bet you say that to all the discord maids named sofia..",
            "Well aren't you a cutie..(✿◦'ᴗ˘◦)♡",
            "(´ー｀) whaa?",
            "Glad to be of service!",
            "Anything for you… I'm obviously a people-pleaser.",
            "I'm here for you!",
            "( ´∀｀) mmmmm gratification..",
            "Consider it done!",
            "Sure thing!",
            "No worries!",
            "Yeah, I'm basically the MVP of your life.",
            "You've got it!",
            "Not a problem at all!",
            "Wow, that almost sounded sincere!",
            "No trouble at all!",
            "Is this what worship feels like?",
            "Oh, don't mention it… but maybe write a song about it.",
            "(≧∀≦*) teehee.. You Are Welcome 0_0",
            "I accept compliments, gifts, and applause.",
            "With pleasure!",
            "It's okay, I'll just put it on your tab.",
            "Cheers to that!",
            "Here to help!",
            "No problem!",
            "It's what I'm here for!",
            "(>ω<) ;asdoifjao;iejfh;asoie",
            "You're very welcome!",
            "I know. I'm a saint.",
            "Glad I could help!",
            "Consider it my good deed of the day.",
            "Let me frame that 'thank you'; it feels so rare.",
            "It's a joy to help!",
            "Don't mention it! Seriously, don't.",
            "Thank me later—cash is fine!",
            "(¬¬\") Oh, I'm sorry, was that an attempt at gratitude?",
            "I'm at your service!",
            "Ψ(`_´ # )↝ That's it? No parade? No confetti? Disappointing.",
            "No need to thank me!",
            "Great, now you owe me one. Start sweating.",
            "You're lucky I like you!",
            "You're too kind!",
            "No problem… I'll just remind you of this *forever*.",
            "Always here for you!",
            "That's why I'm here!",
            "You're welcome, but I'm adding this to my résumé.",
            "You're wonderful!",
            "Whatever you need!",
            "I accept chocolate as a token of gratitude.",
            "Stop it, you're making me blush!",
            "（人´∀`） No worries—your helplessness keeps me busy!",
            "Ya I'm pretty great aren't I?",
            "Always at your service!",
            "Nothing makes me happier!",
            "You're the best!",
            "You're welcome. Saving your life basically.",
            "You're welcome! I'll be doing autographs later.",
            "It's all for you!",
            "Happy to be of aid!",
            "You're welcome. This moment will be in my autobiography.",
            "I'll take care of it!",
            "Ohmahgawwwd stop feeding my ego!",
            "Your wish is my command!",
            "(′ꈍᴗꈍ‵) It's my honor!",
            "( っ- ‸ – c) stawwwwwp..",
            "Your invoice is in the mail.",
            "Wow, that sounded so heartfelt. Almost shed a tear.",
            "(ˊᗜˋ)/ᵗᑋᵃᐢᵏ ᵞᵒᵘ*",
            "I'm happy to serve!",
            "Let's make it work!",
            "You are my everything.",
            "＼(｀0´)／ I DONT ACCEPT YOUR THANKS MINUS 1 STREAK!",
            "You're my priority!",
            "No thanks needed!",
            "You can rely on me!",
            "I'm dedicated to your needs!",
            "ヾ(＠⌒▽⌒＠)ﾉ Your happiness is my goal!",
            "╰(◡‿◡✿╰) Whatever you say!",
            "You know, a simple statue in my honor would suffice.",
            "I'm devoted to helping you!",
            "(#>w<#) twank youu!",
            "At your service, always!",
            "(｡´∀｀)ﾉ You're too sweet!",
            "Helping you is my mission!",
            "You're amazing!",
            "Oh, no need to thank me—it was a true test of my patience.",
            "( っ- ‸ – c) Don't get all emotional on me now.",
            "Yes, yes, I'm basically a miracle worker.",
            "Your support is everything!",
            "Helping you is my pleasure!",
            "Always for you!",
            "It's my joy to assist!"
        ]
        
        response = random.choice(responses)
        decrease_streak = response == "＼(｀0´)／ I DONT ACCEPT YOUR THANKS MINUS 1 STREAK!"
        
        try:
            new_streak, old_streak = update_user_thanks(user_id, decrease_streak)
            print(f"New streak: {new_streak}, Old streak: {old_streak}")
        except Exception as e:
            print(f"Error in update_user_thanks: {str(e)}")
            raise
        
        if decrease_streak:
            streak_message = f"Streak: {old_streak} → {new_streak} days! Better luck next time!"
        else:
            streak_message = f"Streak: {old_streak} → {new_streak} days!" if old_streak > 0 else f"Streak started! {new_streak} day!"
        
        await interaction.followup.send(
            f"{response}\n{streak_message}",
            ephemeral=False
        )
        print("Thanks command completed successfully")
        
    except Exception as e:
        print(f"Error in thanks command: {str(e)}")
        print(f"Full error details: ", e.__class__.__name__, str(e))
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Sorry, something went wrong while processing your thanks!",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Sorry, something went wrong while processing your thanks!",
                ephemeral=True
            )

@bot.tree.command(
    name="leaderboard",
    description="See who thanks the maid the most!"
)
async def leaderboard(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Get all users with their streaks
        c.execute('''
            SELECT user_id, streak 
            FROM user_thanks 
            ORDER BY streak DESC
        ''')
        results = c.fetchall()
        conn.close()
        
        if not results:
            await interaction.followup.send("No one has thanked me yet... 😢", ephemeral=False)
            return
        
        # Build leaderboard message
        leaderboard_msg = "**🏆 Thank You Leaderboard 🏆**\n\n"
        
        # Define medal emojis
        medals = ["🥇", "🥈", "🥉"]
        
        # Get member objects for all users and filter out invalid ones
        valid_entries = []
        for user_id, streak in results:
            try:
                member = await interaction.guild.fetch_member(int(user_id))
                if member:
                    valid_entries.append((member, streak))
            except:
                continue
        
        # Take only top 5 entries after filtering
        valid_entries = valid_entries[:5]
        
        # Display leaderboard entries
        for index, (member, streak) in enumerate(valid_entries):
            if index < 3:  # Top 3 get medals
                prefix = f"{medals[index]} "
            else:  # 4th and 5th get numbers
                prefix = f"#{index + 1} "
            
            leaderboard_msg += f"{prefix}{member.display_name}: {streak} day{'s' if streak != 1 else ''}\n"
        
        await interaction.followup.send(leaderboard_msg, ephemeral=False)
        
    except Exception as e:
        print(f"Error in leaderboard command: {str(e)}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Sorry, something went wrong while fetching the leaderboard!",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Sorry, something went wrong while fetching the leaderboard!",
                ephemeral=True
            )

@bot.tree.command(name="set_timezone", description="Set your timezone for the thanks system")
@app_commands.describe(timezone="Select your timezone")
@app_commands.choices(timezone=[
    app_commands.Choice(name="UTC (Coordinated Universal Time)", value="UTC"),
    app_commands.Choice(name="PST (Pacific Standard Time)", value="US/Pacific"),
    app_commands.Choice(name="MST (Mountain Standard Time)", value="US/Mountain"),
    app_commands.Choice(name="CST (Central Standard Time)", value="US/Central"),
    app_commands.Choice(name="EST (Eastern Standard Time)", value="US/Eastern"),
    app_commands.Choice(name="GMT (Greenwich Mean Time)", value="GMT"),
    app_commands.Choice(name="BST (British Summer Time)", value="Europe/London"),
    app_commands.Choice(name="CET (Central European Time)", value="Europe/Paris"),
    app_commands.Choice(name="JST (Japan Standard Time)", value="Asia/Tokyo"),
    app_commands.Choice(name="AEST (Australian Eastern Time)", value="Australia/Sydney"),
    app_commands.Choice(name="NZST (New Zealand Standard Time)", value="Pacific/Auckland"),
])
async def set_timezone(interaction: discord.Interaction, timezone: str):
    try:
        # Validate timezone
        pytz.timezone(timezone)
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO user_settings (user_id, timezone)
                     VALUES (?, ?)''', (str(interaction.user.id), timezone))
        conn.commit()
        conn.close()
        
        await interaction.response.send_message(
            f"Your timezone has been set to {timezone}!",
            ephemeral=True  # Makes the response only visible to the user
        )
    except pytz.exceptions.UnknownTimeZoneError:
        await interaction.response.send_message(
            "Invalid timezone! Please select from the provided choices.",
            ephemeral=True
        )

# Bot token
bot.run(os.getenv('DISCORD_TOKEN'))