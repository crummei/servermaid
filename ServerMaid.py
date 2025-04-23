import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import asyncio
import os
import sqlite3
from pathlib import Path
import datetime
import random
import pytz
from contextlib import contextmanager
from asyncio import Lock, sleep
import time
import traceback
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
from flask import Flask
import threading
app = Flask(__name__)

@app.route("/")
def home():
	return "Process is running!"

def run_flask():
	app.run(host="0.0.0.0", port=5000)

thread = threading.Thread(target=run_flask)
thread.daemon = True
thread.start()

MIN_MESSAGES_LIMIT = 1    # Minimum messages to keep
MAX_FETCH_LIMIT = 3000    # Maximum messages to fetch at once
PREMIUM_SKU = "1349502955426025562"  # Changed to use the SKU ID
FREE_MAX_MESSAGES = 5000  # Original max message limit
FREE_MAX_CHANNELS = 10     # Maximum channels for free tier
PREMIUM_MAX_MESSAGES = 5000  # New premium message limit
PREMIUM_MAX_CHANNELS = 10    # Maximum channels for premium tier
CACHE_DURATION = 300  # Cache duration in seconds (5 minutes)

def init_database():
	# Create the database
	script_dir = os.path.dirname(os.path.abspath(__file__))
	db_path = os.path.join(script_dir, "server_settings.db")
	conn = sqlite3.connect(db_path)
	c = conn.cursor()
	c.execute('''CREATE TABLE IF NOT EXISTS channel_settings
				 (server_id TEXT, channel_id TEXT, max_messages INTEGER, keep_pinned BOOLEAN,
				  PRIMARY KEY (server_id, channel_id))''')
	# New table for tracking thanks
	c.execute('''CREATE TABLE IF NOT EXISTS user_thanks
				 (user_id TEXT, last_thanks_date TEXT, streak INTEGER,
				  PRIMARY KEY (user_id))''')
	# New table for server settings
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

DB_PATH = init_database()

load_dotenv()
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.dm_messages = False

bot = commands.AutoShardedBot(
	command_prefix="!",
	intents=intents,
	shard_count=6
)

@contextmanager
def get_db_connection():
	conn = sqlite3.connect(DB_PATH)
	try:
		yield conn
	finally:
		conn.close()

class ChannelSettingsCache:
	def __init__(self):
		self._cache = {}
		self._last_updated = {}
	
	def get(self, server_id: str, channel_id: str) -> tuple[int, bool] | None:
		cache_key = (server_id, channel_id)
		if cache_key in self._cache:
			# Check if cache is still valid
			if time.time() - self._last_updated[cache_key] < CACHE_DURATION:
				return self._cache[cache_key]
			# Remove expired cache
			del self._cache[cache_key]
			del self._last_updated[cache_key]
		return None
	
	def set(self, server_id: str, channel_id: str, settings: tuple[int, bool]):
		cache_key = (server_id, channel_id)
		self._cache[cache_key] = settings
		self._last_updated[cache_key] = time.time()
	
	def invalidate(self, server_id: str, channel_id: str):
		cache_key = (server_id, channel_id)
		if cache_key in self._cache:
			del self._cache[cache_key]
			del self._last_updated[cache_key]

channel_settings_cache = ChannelSettingsCache()

def get_channel_settings(server_id: str, channel_id: str):
	cached_settings = channel_settings_cache.get(server_id, channel_id)
	if cached_settings is not None:
		return cached_settings
	
	# If not in cache, get from database
	with get_db_connection() as conn:
		c = conn.cursor()
		c.execute('''SELECT max_messages, keep_pinned FROM channel_settings 
					 WHERE server_id = ? AND channel_id = ?''', (server_id, channel_id))
		settings = c.fetchone()
		if settings:
			channel_settings_cache.set(server_id, channel_id, settings)
		return settings

# Update settings to invalidate cache
def save_channel_settings(server_id: str, channel_id: str, max_messages: int, keep_pinned: bool):
	conn = sqlite3.connect(DB_PATH)
	c = conn.cursor()
	c.execute('''INSERT OR REPLACE INTO channel_settings (server_id, channel_id, max_messages, keep_pinned)
				 VALUES (?, ?, ?, ?)''', (server_id, channel_id, max_messages, keep_pinned))
	conn.commit()
	conn.close()
	channel_settings_cache.invalidate(server_id, channel_id)

def remove_channel_settings(server_id: str, channel_id: str):
	conn = sqlite3.connect(DB_PATH)
	c = conn.cursor()
	c.execute('''DELETE FROM channel_settings WHERE server_id = ? AND channel_id = ?''',
			  (server_id, channel_id))
	conn.commit()
	conn.close()
	channel_settings_cache.invalidate(server_id, channel_id)

def get_managed_channels(server_id: str):
	conn = sqlite3.connect(DB_PATH)
	c = conn.cursor()
	c.execute('''SELECT channel_id, max_messages, keep_pinned FROM channel_settings 
				 WHERE server_id = ?''', (server_id,))
	results = c.fetchall()
	conn.close()
	return results

def check_user_thanks(user_id: str) -> tuple[bool, int]:
	conn = sqlite3.connect(DB_PATH)
	c = conn.cursor()
	
	local_time = get_user_local_time(user_id)
	today = local_time.strftime('%Y-%m-%d')
	
	c.execute('SELECT last_thanks_date, streak FROM user_thanks WHERE user_id = ?', (user_id,))
	result = c.fetchone()
	
	if not result:
		conn.close()
		return False, 0
	
	last_thanks_date, streak = result
	
	last_thanks_dt = datetime.datetime.strptime(last_thanks_date, '%Y-%m-%d').date()
	local_dt = local_time.date()
	
	days_diff = (local_dt - last_thanks_dt).days
	
	if days_diff > 1:
		streak = 0
	
	already_thanked = (last_thanks_date == today)
	conn.close()
	return already_thanked, streak

def update_user_thanks(user_id: str, decrease_streak: bool = False):
	conn = sqlite3.connect(DB_PATH)
	c = conn.cursor()
	
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
			current_streak = new_streak + 1
		elif days_diff == 1:
			new_streak = current_streak + 1
		elif days_diff == 0:
			new_streak = current_streak
		else:
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
	
	c.execute('SELECT timezone FROM user_settings WHERE user_id = ?', (user_id,))
	result = c.fetchone()
	timezone = result[0] if result else 'UTC'
	conn.close()
	
	utc_time = discord.utils.utcnow()
	local_tz = pytz.timezone(timezone)
	local_time = utc_time.astimezone(local_tz)
	return local_time

async def update_server_list():
	"""Update the servers.txt file with current server list"""
	script_dir = os.path.dirname(os.path.abspath(__file__))
	servers_file = os.path.join(script_dir, "servers.txt")
	
	# Get all current servers
	servers = []
	for guild in bot.guilds:
		server_info = f"{guild.name} (ID: {guild.id}) - Members: {guild.member_count}"
		servers.append(server_info)
	
	servers.sort()
	
	try:
		with open(servers_file, 'w', encoding='utf-8') as f:
			f.write("=== Server Maid Monitored Servers ===\n")
			f.write(f"Last Updated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
			f.write(f"Total Servers: {len(servers)}\n\n")
			for server in servers:
				f.write(f"{server}\n")
		logging.info(f"✍️ Updated servers list in {servers_file}")
	except Exception as e:
		logging.info(f"❌ Error writing servers file: {e}")

@bot.event
async def on_ready():
	logging.info(f'✅ Logged in as {bot.user} (ID: {bot.user.id})')
	logging.info(f'🔄 Connected to {len(bot.guilds)} servers')
	logging.info(f'📊 Using {bot.shard_count} shards')
	for guild in bot.guilds:
		logging.info(f'🛠 Server: {guild.name} (ID: {guild.id})')
	try:
		synced = await bot.tree.sync()
		logging.info(f"Synced {len(synced)} command(s)")
	except Exception as e:
		logging.warning(f"Failed to sync commands: {e}")
	
	await update_server_list()
	logging.info("⚡ Ready to clean messages!")

@bot.event
async def on_guild_join(guild):
	"""Called when the bot joins a new server"""
	logging.info(f"🎉 Joined new server: {guild.name} (ID: {guild.id})")
	await update_server_list()

@bot.event
async def on_guild_remove(guild):
	"""Called when the bot leaves a server"""
	logging.info(f"👋 Left server: {guild.name} (ID: {guild.id})")
	await update_server_list()

@bot.event
async def on_shard_ready(shard_id):
	logging.info(f'Shard {shard_id} is ready')

@bot.event
async def on_shard_connect(shard_id):
	logging.info(f'Shard {shard_id} has connected')

@bot.event
async def on_shard_disconnect(shard_id):
	logging.info(f'Shard {shard_id} has disconnected')

@bot.event
async def on_shard_resumed(shard_id):
	logging.info(f'Shard {shard_id} has resumed')

@bot.event
async def on_shard_error(shard_id, error):
	logging.warning(f'An error occurred on shard {shard_id}: {error}')

# Add a command to check shard status
@bot.tree.command(name="shardinfo", description="Get information about the bot's shards")
async def shard_info(interaction: discord.Interaction):
	if not interaction.user.guild_permissions.administrator:
		await interaction.response.send_message("You need administrator permissions to use this command!", ephemeral=True)
		return
		
	shard_info = []
	for shard_id in range(bot.shard_count):
		shard = bot.get_shard(shard_id)
		latency = round(shard.latency * 1000) if shard else None
		status = "Connected" if shard and not shard.is_closed() else "Disconnected"
		guild_count = len([g for g in bot.guilds if g.shard_id == shard_id])
		
		shard_info.append(
			f"Shard {shard_id}:\n"
			f"  Status: {status}\n"
			f"  Latency: {latency}ms\n"
			f"  Guilds: {guild_count}"
		)
	
	message = "**Shard Information:**\n\n" + "\n\n".join(shard_info)
	await interaction.response.send_message(message, ephemeral=True)

async def check_premium_status(guild_id: str) -> bool:
	"""Check if a guild has the premium subscription"""
	try:
		with get_db_connection() as conn:
			c = conn.cursor()
			c.execute('''SELECT setting_value FROM server_settings 
						 WHERE guild_id = ? AND setting_name = 'premium_sku' ''', (guild_id,))
			result = c.fetchone()
			if result and result[0] == PREMIUM_SKU:
				return True
		
		guild = bot.get_guild(int(guild_id))
		if guild:
			entitlements = await bot.application.fetch_guild_entitlements(guild.id)
			for entitlement in entitlements:
				if str(entitlement.sku_id) == PREMIUM_SKU and not entitlement.consumed:
					# Save to database if not already there
					with get_db_connection() as conn:
						c = conn.cursor()
						c.execute('''INSERT OR REPLACE INTO server_settings
									(guild_id, setting_name, setting_value)
									VALUES (?, ?, ?)''',
								 (guild_id, 'premium_sku', PREMIUM_SKU))
						conn.commit()
					return True
		
		return False
	except Exception as e:
		logging.warning(f"Error checking premium status: {str(e)}")
		return False

def get_server_limits(guild_id: str) -> tuple[int, int]:
	"""Get the message and channel limits based on premium status"""
	try:
		is_premium = check_premium_status(guild_id)
		
		current_channels = get_managed_channels(guild_id)
		current_channel_count = len(current_channels)
		
		if is_premium:
			for channel_id, max_messages, keep_pinned in current_channels:
				if max_messages > PREMIUM_MAX_MESSAGES:
					save_channel_settings(guild_id, channel_id, PREMIUM_MAX_MESSAGES, keep_pinned)
			return PREMIUM_MAX_MESSAGES, PREMIUM_MAX_CHANNELS
		else:
			for channel_id, max_messages, keep_pinned in current_channels:
				if max_messages > FREE_MAX_MESSAGES:
					save_channel_settings(guild_id, channel_id, FREE_MAX_MESSAGES, keep_pinned)
			
			if current_channel_count > FREE_MAX_CHANNELS:
				excess_channels = current_channels[FREE_MAX_CHANNELS:]
				for channel_id, _, _ in excess_channels:
					remove_channel_settings(guild_id, channel_id)
			
			return FREE_MAX_MESSAGES, FREE_MAX_CHANNELS
	except Exception as e:
		logging.warning(f"Error in get_server_limits: {str(e)}")
		return FREE_MAX_MESSAGES, FREE_MAX_CHANNELS

@bot.tree.command(name="configure", description="Configure the maid bot for a specific channel")
@app_commands.describe(
	channel="The channel to manage",
	max_messages="Maximum number of messages to keep in the channel",
	keep_pinned="Whether to preserve pinned messages (true/false)"
)
async def configure(interaction: discord.Interaction, channel: discord.TextChannel, max_messages: int, keep_pinned: bool):
	try:
		max_messages_limit, max_channels = get_server_limits(str(interaction.guild_id))
		
		current_channels = get_managed_channels(str(interaction.guild_id))
		if len(current_channels) >= max_channels and str(channel.id) not in [c[0] for c in current_channels]:
			await interaction.response.send_message(
				f"You've reached your maximum channel limit ({max_channels}). " +
				("Upgrade to Premium to manage more channels!" if max_channels == FREE_MAX_CHANNELS else ""),
				ephemeral=True
			)
			return
		
		if max_messages > max_messages_limit:
			await interaction.response.send_message(
				f"Maximum messages cannot exceed {max_messages_limit}. " +
				("Upgrade to Premium for a higher limit!" if max_messages_limit == FREE_MAX_MESSAGES else ""),
				ephemeral=True
			)
			return
		
		if max_messages < MIN_MESSAGES_LIMIT:
			await interaction.response.send_message(
				f"Minimum messages cannot be less than {MIN_MESSAGES_LIMIT}",
				ephemeral=True
			)
			return
		
		permissions = channel.permissions_for(interaction.guild.me)
		if not (permissions.manage_messages and permissions.read_message_history):
			await interaction.response.send_message(
				"I need 'Manage Messages' and 'Read Message History' permissions in this channel!",
				ephemeral=True
			)
			return
		
		if not interaction.user.guild_permissions.administrator:
			await interaction.response.send_message("You need administrator permissions to use this command!", ephemeral=True)
			return
		
		save_channel_settings(str(interaction.guild_id), str(channel.id), max_messages, keep_pinned)
		
		await interaction.response.send_message(
			f"Channel {channel.mention} configured with max messages: {max_messages}, keep pinned: {keep_pinned}\nStarting initial cleanup...",
			ephemeral=True
		)
		
		messages = []
		async for msg in channel.history(limit=MAX_FETCH_LIMIT):
			await message_fetcher.acquire()
			messages.append(msg)
		
		if keep_pinned:
			messages = [msg for msg in messages if not msg.pinned]
		messages.sort(key=lambda x: x.created_at)
	
	#if len(messages) > max_messages:
	if len(messages) > 0:
		#to_delete = messages[:len(messages) - max_messages]
			to_delete = messages[:len(messages) - 0]
			deleted, failed = await delete_messages_safely(to_delete, channel)
			
			await interaction.followup.send(
				f"Initial cleanup complete. Deleted {deleted} messages" + 
				(f", failed to delete {failed} messages." if failed > 0 else "."),
				ephemeral=True
			)
	
	except discord.errors.Forbidden as e:
		await interaction.response.send_message(
			"I don't have the required permissions to perform this action.",
			ephemeral=True
		)
	except discord.errors.HTTPException as e:
		await interaction.response.send_message(
			"There was an error communicating with Discord. Please try again.",
			ephemeral=True
		)
	except Exception as e:
		logging.warning(f"Error in configure command: {str(e)}")
		await interaction.response.send_message(
			"An unexpected error occurred. Please try again later.",
			ephemeral=True
		)
	
	message_count_cache.invalidate(str(channel.id))

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
	
	message_count_cache.invalidate(str(channel.id))

class MessageCountCache:
	def __init__(self):
		self._cache = {}
		self._last_updated = {}
		self.lock = Lock()
	
	async def get_message_count(self, channel_id: str, channel) -> int:
		"""Get message count from cache or fetch if needed"""
		async with self.lock:
			now = time.time()
			if channel_id in self._cache:
				return self._cache[channel_id]
			
			count = 0
			async for _ in channel.history(limit=None):
				count += 1
			
			self._cache[channel_id] = count
			self._last_updated[channel_id] = now
			return count
	
	def increment_count(self, channel_id: str):
		"""Increment message count for a channel"""
		if channel_id in self._cache:
			self._cache[channel_id] += 1
	
	def set_count(self, channel_id: str, count: int):
		"""Set exact message count for a channel"""
		self._cache[channel_id] = count
	
	def invalidate(self, channel_id: str):
		"""Remove channel from cache"""
		if channel_id in self._cache:
			del self._cache[channel_id]
			if channel_id in self._last_updated:
				del self._last_updated[channel_id]

message_count_cache = MessageCountCache()

@bot.event
async def on_message(message):
	if message.guild is None or message.author == bot.user:
		return
		
	settings = get_channel_settings(str(message.guild.id), str(message.channel.id))
	if not settings:
		return
	
	max_messages, keep_pinned = settings
	channel_id = str(message.channel.id)
	
	try:
		current_count = await message_count_cache.get_message_count(channel_id, message.channel)
		message_count_cache.increment_count(channel_id)
		
		if current_count + 1 > max_messages:
			logging.info(f"\n=== Starting message cleanup for channel {message.channel.name} ===")
			logging.info(f"Current messages: {current_count + 1}, Max allowed: {max_messages}")
			
			messages_to_delete = []
			async for msg in message.channel.history(limit=current_count + 1):
				if not (keep_pinned and msg.pinned):
					messages_to_delete.append(msg)
			
			messages_to_delete.sort(key=lambda x: x.created_at)
			
			# Only delete the oldest messages that exceed our limit
			to_delete = messages_to_delete[:-max_messages] if messages_to_delete else []
			
			if to_delete:
				logging.info(f"Deleting {len(to_delete)} oldest messages to maintain limit of {max_messages}")
				deleted, failed = await delete_messages_safely(to_delete, message.channel)
				
				# Update cache with accurate count - count all messages including pinned ones
				actual_count = 0
				async for _ in message.channel.history(limit=None):
					actual_count += 1
				
				message_count_cache.set_count(channel_id, actual_count)
				
				logging.info(f"New message count: {actual_count}")
				logging.info(f"Channel {message.channel.name} (ID: {message.channel.id}) in server {message.guild.name} (ID: {message.guild.id}) is within message limit ({actual_count}/{max_messages})")
		else:
			logging.info(f"Channel {message.channel.name} (ID: {message.channel.id}) in server {message.guild.name} (ID: {message.guild.id}) is within message limit ({current_count + 1}/{max_messages})")
				
	except Exception as e:
		logging.warning(f"Error in message handler: {e}")
		logging.warning(f"Full error: {traceback.format_exc()}")
		message_count_cache.invalidate(channel_id)

@bot.event
async def on_guild_join(guild):
	"""Sends a welcome message when the bot joins a new server"""
	logging.info(f"Joined new guild: {guild.name} (ID: {guild.id})")
	
	target_channel = None
	
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
			logging.info(f"Sent welcome message in {target_channel.name}")  # Debug log
		except discord.Forbidden:
			logging.warning(f"Failed to send welcome message - Missing permissions in {target_channel.name}")
		except Exception as e:
			logging.warning(f"Error sending welcome message: {str(e)}")
	else:
		logging.info(f"Could not find a suitable channel to send welcome message in {guild.name}")

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
			keep_pinned_str = "True" if keep_pinned else "False"
			message += f"• {channel.mention}: Max messages: {max_messages}, Keep pinned: {keep_pinned_str}\n"
	
	await interaction.response.send_message(message, ephemeral=True)

@bot.tree.command(
	name="thanks",
	description="Thank the bot for its service!"
)
async def thanks(interaction: discord.Interaction):
	try:
		logging.info("Starting thanks command...")
		
		try:
			await interaction.response.defer(ephemeral=False)
		except discord.errors.NotFound:
			pass
			
		logging.info("Response deferred")
		
		user_id = str(interaction.user.id)
		
		# Validate user exists in guild
		try:
			member = await interaction.guild.fetch_member(interaction.user.id)
		except discord.NotFound:
			await interaction.followup.send("Could not verify your server membership.", ephemeral=True)
			return
		
		logging.info(f"Member validated: {member.display_name}")
		
		with get_db_connection() as conn:
			c = conn.cursor()
			c.execute('SELECT timezone FROM user_settings WHERE user_id = ?', (user_id,))
			timezone_result = c.fetchone()
			
		if not timezone_result:
			await interaction.followup.send(
				"Please set your timezone first using `/set_timezone`!",
				ephemeral=True
			)
			return
		
		logging.info(f"Timezone checked: {timezone_result[0]}")
		
		already_thanked, current_streak = check_user_thanks(user_id)
		logging.info(f"Thanks check - Already thanked: {already_thanked}, Current streak: {current_streak}")  # Debug log
		
		if already_thanked:
			await interaction.followup.send(
				f"You've already thanked me today! Current streak: {current_streak} days",
				ephemeral=True
			)
			return
			
		# If we get here, they havent thanked today, proceed with updating thanks
		responses = [
			"Noo thank you!!",
			"You are too kind ^^",
			"Of course!",
			":))))))))))))))",
			"Happy to help!",
			"I love you.",
			"I bet you say that to all the discord maids named Sofia..",
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
		
		logging.info(f"Selected response: {response}")
		
		try:
			new_streak, old_streak = update_user_thanks(user_id, decrease_streak)
			logging.info(f"Updated thanks - New streak: {new_streak}, Old streak: {old_streak}")
		except Exception as e:
			logging.warning(f"Error in update_user_thanks: {str(e)}")
			await interaction.followup.send("An error occurred while updating your streak.", ephemeral=True)
			return
		
		if decrease_streak:
			streak_message = f"Streak: {old_streak} → {new_streak} days! Better luck next time!"
		else:
			streak_message = f"Streak: {old_streak} → {new_streak} days!" if old_streak > 0 else f"Streak started! {new_streak} day!"
		
		final_message = f"{response}\n{streak_message}"
		logging.info(f"Sending final message: {final_message}")
		
		await interaction.followup.send(final_message, ephemeral=False)
		logging.info("Command completed successfully")
		
	except Exception as e:
		logging.warning(f"Error in thanks command: {e.__class__.__name__}: {str(e)}")
		traceback.print_exc()
		# Only send error message if we havent sent a response yet
		if not interaction.response.is_done():
			try:
				await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)
			except:
				pass

@bot.tree.command(
	name="leaderboard",
	description="See who thanks the maid the most!"
)
async def leaderboard(interaction: discord.Interaction):
	try:
		await interaction.response.defer(ephemeral=False, thinking=True)
		
		with get_db_connection() as conn:
			c = conn.cursor()
			
			c.execute('''
				SELECT user_id, streak 
				FROM user_thanks 
				ORDER BY streak DESC
			''')
			results = c.fetchall()
		
		if not results:
			await interaction.followup.send("No one has thanked me yet... 😢", ephemeral=False)
			return
		
		leaderboard_msg = "**🏆 Thank You Leaderboard 🏆**\n\n"
		
		medals = ["🥇", "🥈", "🥉"]
		
		valid_entries = []
		for user_id, streak in results:
			try:
				member = await interaction.guild.fetch_member(int(user_id))
				if member:
					valid_entries.append((member, streak))
			except:
				continue
		
		valid_entries = valid_entries[:5]
		
		for index, (member, streak) in enumerate(valid_entries):
			if index < 3:
				prefix = f"{medals[index]} "
			else:
				prefix = f"#{index + 1} "
			
			leaderboard_msg += f"{prefix}{member.display_name}: {streak} day{'s' if streak != 1 else ''}\n"
		
		await interaction.followup.send(leaderboard_msg, ephemeral=False)
		
	except Exception as e:
		logging.warning(f"Error in leaderboard command: {str(e)}")
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
		pytz.timezone(timezone)
		
		conn = sqlite3.connect(DB_PATH)
		c = conn.cursor()
		c.execute('''INSERT OR REPLACE INTO user_settings (user_id, timezone)
					 VALUES (?, ?)''', (str(interaction.user.id), timezone))
		conn.commit()
		conn.close()
		
		await interaction.response.send_message(
			f"Your timezone has been set to {timezone}!",
			ephemeral=True
		)
	except pytz.exceptions.UnknownTimeZoneError:
		await interaction.response.send_message(
			"Invalid timezone! Please select from the provided choices.",
			ephemeral=True
		)

class RateLimiter:
	def __init__(self, requests_per_second: float, max_backoff: float = 300.0):
		self.base_delay = max(1.0 / requests_per_second, 1.0)
		self.current_delay = self.base_delay
		self.max_backoff = max_backoff
		self.last_request = 0
		self.consecutive_429s = 0
		self.lock = Lock()
		self._cache = {}
	
	async def acquire(self):
		async with self.lock:
			now = time.time()
			if self.last_request:
				wait_time = max(
					self.current_delay - (now - self.last_request),
					self.base_delay
				)
				if wait_time > 0:
					logging.info(f"Rate limiter waiting for {wait_time:.2f} seconds...")
					await sleep(wait_time)
			self.last_request = time.time()
	
	def increase_backoff(self, retry_after: float = None):
		self.consecutive_429s += 1
		if retry_after is not None:
			self.current_delay = min(
				retry_after + 1.0,
				self.max_backoff
			)
		else:
	
			self.current_delay = min(
				self.base_delay * (4 ** self.consecutive_429s),
				self.max_backoff
			)
		logging.warning(f"Rate limited! Increasing backoff delay to {self.current_delay:.2f} seconds")
	
	def reset_backoff(self):
		if self.consecutive_429s > 0:
			logging.info("Resetting rate limit backoff")
			self.consecutive_429s = 0
			self.current_delay = self.base_delay

message_deleter = RateLimiter(0.5)
message_fetcher = RateLimiter(1.0)

async def delete_messages_safely(messages_to_delete, channel):
	"""Safely delete messages with rate limiting and error handling"""
	deleted_count = 0
	failed_count = 0
	
	logging.info(f"\n=== Starting message deletion process in channel: {channel.name} (ID: {channel.id}) ===")
	logging.info(f"Server: {channel.guild.name} (ID: {channel.guild.id})")
	
	# Group messages by age
	recent_messages = []
	old_messages = []
	
	for msg in messages_to_delete:
		if (discord.utils.utcnow() - msg.created_at).days < 14:
			recent_messages.append(msg)
		else:
			old_messages.append(msg)
	
	logging.info(f"Messages to process - Recent: {len(recent_messages)}, Old: {len(old_messages)}")
	
	# Bulk delete recent messages in chunks
	chunks = [recent_messages[i:i + 50] for i in range(0, len(recent_messages), 50)]
	for i, chunk in enumerate(chunks, 1):
		try:
			logging.info(f"Processing chunk {i}/{len(chunks)} ({len(chunk)} messages)")
			await message_deleter.acquire()
			await channel.delete_messages(chunk)
			deleted_count += len(chunk)
			message_deleter.reset_backoff()
			logging.info(f"Successfully deleted chunk {i}")
			await asyncio.sleep(5.0)
		except discord.errors.HTTPException as e:
			logging.warning(f"HTTP error in chunk {i}: {str(e)}")
			if e.status == 429:
				retry_after = e.retry_after if hasattr(e, 'retry_after') else 30.0
				message_deleter.increase_backoff(retry_after)
				wait_time = retry_after + 5.0
				logging.warning(f"Rate limited. Waiting {wait_time} seconds...")
				await asyncio.sleep(wait_time)
				try:
					await channel.delete_messages(chunk)
					deleted_count += len(chunk)
					logging.info(f"Successfully deleted chunk {i} after rate limit")
				except Exception as inner_e:
					logging.warning(f"Failed to delete chunk after rate limit: {inner_e}")
					failed_count += len(chunk)
					await asyncio.sleep(30.0)
			else:
				logging.warning(f"Error deleting messages: {e}")
				failed_count += len(chunk)
				await asyncio.sleep(30.0)
		except Exception as e:
			logging.warning(f"Unexpected error in chunk {i}: {str(e)}")
			failed_count += len(chunk)
			await asyncio.sleep(30.0)
	
	if old_messages:
		logging.info(f"\nProcessing {len(old_messages)} old messages")
		for i, msg in enumerate(old_messages, 1):
			try:
				await message_deleter.acquire()
				await msg.delete()
				deleted_count += 1
				message_deleter.reset_backoff()
				await asyncio.sleep(5.0)
			except discord.errors.HTTPException as e:
				if e.status == 429:
					retry_after = e.retry_after if hasattr(e, 'retry_after') else 30.0
					message_deleter.increase_backoff(retry_after)
					wait_time = retry_after + 5.0
					logging.warning(f"Rate limited. Waiting {wait_time} seconds...")
					await asyncio.sleep(wait_time)
					try:
						await msg.delete()
						deleted_count += 1
						logging.info(f"Successfully deleted message {i} after rate limit")
					except:
						failed_count += 1
						await asyncio.sleep(30.0)
				else:
					failed_count += 1
					await asyncio.sleep(30.0)
			except Exception as e:
				logging.warning(f"Error deleting old message {i}: {e}")
				failed_count += 1
				await asyncio.sleep(30.0)
	
	logging.info(f"\n=== Deletion process complete for channel {channel.name} (ID: {channel.id}) in server {channel.guild.name} (ID: {channel.guild.id}) ===")
	logging.info(f"Final results - Deleted: {deleted_count}, Failed: {failed_count}")
	return deleted_count, failed_count

@bot.tree.command(
	name="subscribe",
	description="Get information about Server Maid Premium subscription"
)
@app_commands.guild_only()
async def subscribe(interaction: discord.Interaction):
	logging.info(f"Subscribe command triggered by {interaction.user} in {interaction.guild}")
	
	if not interaction.user.guild_permissions.administrator:
		await interaction.response.send_message("You need administrator permissions to manage subscriptions!", ephemeral=True)
		return
	
	is_premium = await check_premium_status(str(interaction.guild_id))
	if is_premium:
		await interaction.response.send_message(
			"This server already has Server Maid Premium! 🎉\n"
			f"Current limits:\n"
			f"• Max messages per channel: {PREMIUM_MAX_MESSAGES}\n"
			f"• Max managed channels: {PREMIUM_MAX_CHANNELS}",
			ephemeral=True
		)
		return
	
	embed = discord.Embed(
		title="Server Maid Premium ✨",
		description="Upgrade your server's cleaning capabilities!",
		color=discord.Color.gold()
	)
	
	embed.add_field(
		name="Premium Features",
		value=f"• Increased message limit: {FREE_MAX_MESSAGES} → {PREMIUM_MAX_MESSAGES}\n"
			  f"• More managed channels: {FREE_MAX_CHANNELS} → {PREMIUM_MAX_CHANNELS}",
		inline=False
	)
	
	embed.add_field(
		name="Current Status",
		value="🔓 Free Tier",
		inline=False
	)
	
	embed.add_field(
		name="How to Subscribe",
		value="Click on my profile and check out the Store tab to purchase Server Maid Premium!",
		inline=False
	)
	
	await interaction.response.send_message(
		embed=embed,
		ephemeral=True
	)

@bot.event
async def on_application_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
	"""Handle command errors globally"""
	logging.warning(f"Command error: {str(error)}")
	if not interaction.response.is_done():
		await interaction.response.send_message(
			"An error occurred while processing your command.",
			ephemeral=True
		)

@bot.event
async def on_entitlement_create(entitlement: discord.Entitlement):
	"""Handle new entitlements (premium purchases)"""
	try:
		if str(entitlement.sku_id) == PREMIUM_SKU:
			with get_db_connection() as conn:
				c = conn.cursor()
				c.execute('''INSERT OR REPLACE INTO server_settings
							(guild_id, setting_name, setting_value)
							VALUES (?, ?, ?)''',
						 (str(entitlement.guild_id), 'premium_sku', PREMIUM_SKU))
				conn.commit()
			
			guild = bot.get_guild(entitlement.guild_id)
			if guild:
				for channel in guild.text_channels:
					if channel.permissions_for(guild.me).send_messages:
						await channel.send(
							"🎉 **Thank you for upgrading to Server Maid Premium!**\n"
							f"• Maximum messages per channel increased to {PREMIUM_MAX_MESSAGES}\n"
							f"• Maximum managed channels increased to {PREMIUM_MAX_CHANNELS}"
						)
						break
	except Exception as e:
		logging.warning(f"Error handling entitlement create: {str(e)}")

@bot.event
async def on_entitlement_delete(entitlement: discord.Entitlement):
	"""Handle entitlement deletions (premium expiration/cancellation)"""
	try:
		if str(entitlement.sku_id) == PREMIUM_SKU:
			with get_db_connection() as conn:
				c = conn.cursor()
				c.execute('''DELETE FROM server_settings
							WHERE guild_id = ? AND setting_name = 'premium_sku' ''',
						 (str(entitlement.guild_id),))
				conn.commit()
			
			guild = bot.get_guild(entitlement.guild_id)
			if guild:
				for channel in guild.text_channels:
					if channel.permissions_for(guild.me).send_messages:
						await channel.send(
							"⚠️ **Server Maid Premium has expired**\n"
							f"• Maximum messages per channel reduced to {FREE_MAX_MESSAGES}\n"
							f"• Maximum managed channels reduced to {FREE_MAX_CHANNELS}"
						)
						break
	except Exception as e:
		logging.warning(f"Error handling entitlement delete: {str(e)}")

try:
	bot.run(
		os.environ.get('DISCORD_TOKEN'),
		reconnect=True
	)
except Exception as e:
	logging.warning(f"Failed to start bot: {e}")
