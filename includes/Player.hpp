#pragma once
#include <string>
#include <deque>
#include <chrono>
#include <unistd.h>

typedef std::chrono::steady_clock Clock;

class Player {
	public:
		enum Direction { NORTH = 0, EAST = 1, SOUTH = 2, WEST = 3 };

		struct InventoryPly {
			int food = 10;
			int linemate = 0;
			int deraumere = 0;
			int sibur = 0;
			int mendiane = 0;
			int phiras = 0;
			int thystame = 0;
		};

		static const size_t MAX_PENDING_COMMANDS = 10;
		static const size_t MAX_LINE_LENGTH = 8192;

	private:
		int m_fd;
		int m_id = -1;
		std::string m_in_buffer;
		std::string m_out_buffer;
		std::deque<std::string> m_commands;
		Clock::time_point m_action_end_time{};
		Clock::time_point m_death_time{};
		InventoryPly m_inventory;

		int m_x = 0;
		int m_y = 0;
		Direction m_orientation = NORTH;
		size_t m_level = 1;
		std::string m_team;
		bool m_is_authenticated = false;
		bool m_is_graphic = false;
		bool m_is_frozen = false;
		bool m_is_dead = false;

	public:
		explicit Player(int fd);
		Player(const Player &other) = delete;
		Player& operator=(const Player &other) = delete;
		~Player();

		// Network buffers
		void append_input(const char *data, size_t len);
		bool pop_line(std::string &line);
		void queue_output(const std::string &msg);
		std::string& get_output(void) { return m_out_buffer; }
		bool has_output(void) const { return !m_out_buffer.empty(); }

		// Command queue (max 10 pending requests, extra ones are ignored)
		bool push_command(const std::string &cmd);
		const std::string& front_command(void) const { return m_commands.front(); }
		void pop_command(void) { m_commands.pop_front(); }
		bool has_command(void) const { return !m_commands.empty(); }

		// Timing
		void set_action_end_time(Clock::time_point tp) { m_action_end_time = tp; }
		Clock::time_point get_action_end_time(void) const { return m_action_end_time; }
		void set_death_time(Clock::time_point tp) { m_death_time = tp; }
		Clock::time_point get_death_time(void) const { return m_death_time; }
		void add_life(Clock::duration d) { m_death_time += d; }

		InventoryPly& get_inventoryPly(void) { return m_inventory; }

		// Player number "#n" of the graphic protocol, unique for the whole game
		int getId() const { return m_id; }
		void setId(int id) { m_id = id; }

		int getX() const { return m_x; }
		int getY() const { return m_y; }
		void setX(int x) { m_x = x; }
		void setY(int y) { m_y = y; }

		Direction getOrientation() const { return m_orientation; }
		void setOrientation(Direction dir) { m_orientation = dir; }

		size_t getLevel() const { return m_level; }
		void setLevel(size_t lvl) { m_level = lvl; }

		const std::string& getTeam() const { return m_team; }
		void setTeam(const std::string& team) { m_team = team; m_is_authenticated = true; }

		bool isAuthenticated() const { return m_is_authenticated; }
		bool isGraphic() const { return m_is_graphic; }
		void setGraphic(bool state) { m_is_graphic = state; m_is_authenticated = true; }
		bool isFrozen() const { return m_is_frozen; }
		void setFrozen(bool state) { m_is_frozen = state; }
		bool isDead() const { return m_is_dead; }
		void setDead(void) { m_is_dead = true; m_commands.clear(); }

		// A player in the game (not a graphic client, not waiting for handshake, not dead)
		bool isPlayer() const { return m_is_authenticated && !m_is_graphic && !m_is_dead; }
};
