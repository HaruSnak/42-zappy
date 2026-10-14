#pragma once
#include <vector>
#include <utility>
#include <cstdlib>
#include <algorithm>

class World {
public:
	struct Tile {
		int food = 0;
		int linemate = 0;
		int deraumere = 0;
		int sibur = 0;
		int mendiane = 0;
		int phiras = 0;
		int thystame = 0;
	};

private:
	int m_width;
	int m_height;
	std::vector<std::vector<Tile>> m_grid;

	// Resource generation rule: each resource keeps a density per tile over the
	// whole map (food is common, thystame is rare). Missing units are placed on
	// random tiles, so the world never runs dry but never overflows either.
	static double density(int r) {
		static const double d[7] = {0.5, 0.3, 0.15, 0.1, 0.1, 0.08, 0.05};
		return d[r];
	}
	static int &field(Tile &t, int r) {
		int *f[7] = {&t.food, &t.linemate, &t.deraumere, &t.sibur, &t.mendiane, &t.phiras, &t.thystame};
		return *f[r];
	}

public:
	World() : m_width(0), m_height(0) {}
	World(int width, int height) : m_width(width), m_height(height) {
		m_grid.resize(m_height, std::vector<Tile>(m_width));
	}

	int clampX(int x) const { return ((x % m_width) + m_width) % m_width; }
	int clampY(int y) const { return ((y % m_height) + m_height) % m_height; }

	Tile& getTile(int x, int y) {
		return m_grid[clampY(y)][clampX(x)];
	}

	void spawnResources() {
		refillResources();
	}

	// Tops every resource up to its target quantity on the map.
	// Returns the coordinates of the tiles that changed.
	std::vector<std::pair<int, int>> refillResources() {
		std::vector<std::pair<int, int>> changed;
		int area = m_width * m_height;
		for (int r = 0; r < 7; ++r) {
			int target = static_cast<int>(area * density(r));
			if (target < 1)
				target = 1;
			int total = 0;
			for (int y = 0; y < m_height; ++y)
				for (int x = 0; x < m_width; ++x)
					total += field(m_grid[y][x], r);
			for (; total < target; ++total) {
				int x = rand() % m_width;
				int y = rand() % m_height;
				field(m_grid[y][x], r)++;
				changed.push_back(std::make_pair(x, y));
			}
		}
		std::sort(changed.begin(), changed.end());
		changed.erase(std::unique(changed.begin(), changed.end()), changed.end());
		return changed;
	}
};
